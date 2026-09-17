"""The per-project integration branch: where verified agent work lands.

Phase 3's one write destination. Every rule here exists to keep an unattended
merge reversible and away from the owner's own checkout:

* Work merges into ``cortex/integration`` only. The branch name is decided in
  code; no caller can ask for ``main``/``master``, and nothing here pushes.
* The merge happens in a dedicated worktree (``_integration``), never in the
  canonical repository, so the owner's working tree, index and current branch
  are untouched whatever the scheduler is doing.
* Merges are ``--no-ff``, so one merge is one revertible commit even when the
  branch could have fast-forwarded.
* A conflicting merge is aborted and reported. Cortex never resolves a conflict
  on its own; that is an owner decision and goes to the inbox.

The same worktree is where the project's test command runs against the *merged*
result, which is the only place that question can be answered honestly.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config, gitutil

BRANCH = "cortex/integration"
PROTECTED_BRANCHES = frozenset({"main", "master", "trunk", "develop"})
WORKSPACE_DIR = "_integration"

_CONFLICT_LINE = re.compile(r"^(?:CONFLICT|Auto-merging failed|error: )", re.M)


class IntegrationError(RuntimeError):
    """The integration branch could not be prepared or updated safely."""


@dataclass(frozen=True)
class Workspace:
    """A checkout of ``cortex/integration`` that only Cortex writes to."""

    path: Path
    branch: str
    base: str
    created: bool


@dataclass(frozen=True)
class MergeOutcome:
    status: str              # merged | conflict | up_to_date | failed
    branch: str
    commit: str | None = None
    conflicts: tuple[str, ...] = ()
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"merged", "up_to_date"}


def base_branch(repo: str | Path) -> str:
    """The branch the integration branch is cut from: the repo's own default.

    Read from the repository rather than assumed, because these repositories
    disagree: some are on ``master``, some on ``main``.
    """
    head = gitutil.branch(repo)
    if head and head != BRANCH:
        return head
    for candidate in ("main", "master"):
        if _git(repo, "show-ref", "--verify", f"refs/heads/{candidate}").returncode == 0:
            return candidate
    raise IntegrationError(f"cannot determine the default branch of {repo}")


def ensure(repo: str | Path, project_id: str) -> Workspace:
    """Create (if needed) the integration branch and its private worktree."""
    repo = Path(repo)
    if not gitutil.is_repo(repo):
        raise IntegrationError(f"not a Git repository: {repo}")
    base = base_branch(repo)
    target = config.work_root() / project_id / WORKSPACE_DIR

    if target.exists():
        if not gitutil.is_repo(target):
            raise IntegrationError(
                f"integration workspace exists but is not a repository: {target}"
            )
        checked_out = gitutil.branch(target)
        if checked_out != BRANCH:
            raise IntegrationError(
                f"integration workspace {target} is on '{checked_out}', not '{BRANCH}'"
            )
        return Workspace(path=target, branch=BRANCH, base=base, created=False)

    exists = _git(repo, "show-ref", "--verify", f"refs/heads/{BRANCH}").returncode == 0
    if not exists:
        created = _git(repo, "branch", BRANCH, base)
        if created.returncode != 0:
            raise IntegrationError(_message(created, "git branch failed"))
    target.parent.mkdir(parents=True, exist_ok=True)
    added = _git(repo, "worktree", "add", str(target), BRANCH)
    if added.returncode != 0:
        raise IntegrationError(_message(added, "git worktree add failed"))
    return Workspace(path=target, branch=BRANCH, base=base, created=True)


def refresh(workspace: Workspace) -> str:
    """Bring the integration branch up to its base, fast-forward only.

    If the owner has moved the base branch on, unmerged Cortex work should be
    tested against that newer code. A real merge could conflict, and resolving
    it is exactly what this module refuses to do unattended, so anything other
    than a clean fast-forward is reported and left alone.
    """
    proc = _git(workspace.path, "merge", "--ff-only", workspace.base)
    if proc.returncode == 0:
        return "up_to_date" if "Already up to date" in proc.stdout else "fast_forwarded"
    _git(workspace.path, "merge", "--abort")
    return "diverged"


def merge(
    workspace: Workspace,
    source_branch: str,
    *,
    message: str,
    author: str = "Cortex <cortex@localhost>",
) -> MergeOutcome:
    """Merge a verified task branch into the integration branch with --no-ff."""
    if source_branch in PROTECTED_BRANCHES or source_branch == workspace.base:
        raise IntegrationError(f"refusing to merge the base branch '{source_branch}'")
    current = gitutil.branch(workspace.path)
    if current != BRANCH:
        raise IntegrationError(
            f"integration workspace is on '{current}', not '{BRANCH}'; refusing to merge"
        )
    dirty = gitutil.status_summary(workspace.path)
    if dirty.dirty:
        raise IntegrationError(
            "integration workspace has uncommitted changes; resolve it before merging "
            f"({dirty.modified} modified, {dirty.untracked} untracked)"
        )
    if _git(workspace.path, "show-ref", "--verify", f"refs/heads/{source_branch}").returncode != 0:
        raise IntegrationError(f"no such branch to merge: {source_branch}")

    before = gitutil.head(workspace.path)
    proc = _git(
        workspace.path,
        "-c", f"user.name={author.split(' <')[0]}",
        "-c", f"user.email={author.split('<')[-1].rstrip('>')}",
        "merge", "--no-ff", "--no-edit", "-m", message, source_branch,
    )
    if proc.returncode == 0:
        after = gitutil.head(workspace.path)
        if after == before:
            return MergeOutcome("up_to_date", source_branch, commit=after)
        return MergeOutcome("merged", source_branch, commit=after)

    conflicts = _conflicted_paths(workspace.path)
    _git(workspace.path, "merge", "--abort")
    restored = gitutil.head(workspace.path)
    if restored != before:  # pragma: no cover - git abort restores HEAD
        raise IntegrationError(
            f"merge abort left {workspace.path} at {restored}, expected {before}"
        )
    status = "conflict" if conflicts or _CONFLICT_LINE.search(proc.stdout + proc.stderr) else "failed"
    return MergeOutcome(
        status, source_branch, conflicts=tuple(conflicts),
        detail=_message(proc, "git merge failed"),
    )


def revert(workspace: Workspace, commit: str) -> MergeOutcome:
    """Undo one integration merge, leaving the rest of the branch intact."""
    proc = _git(
        workspace.path,
        "-c", "user.name=Cortex", "-c", "user.email=cortex@localhost",
        "revert", "--no-edit", "-m", "1", commit,
    )
    if proc.returncode == 0:
        return MergeOutcome("merged", workspace.branch, commit=gitutil.head(workspace.path))
    conflicts = _conflicted_paths(workspace.path)
    _git(workspace.path, "revert", "--abort")
    return MergeOutcome(
        "conflict" if conflicts else "failed", workspace.branch,
        conflicts=tuple(conflicts), detail=_message(proc, "git revert failed"),
    )


def rollback(workspace: Workspace, commit: str) -> None:
    """Discard everything on the integration branch after ``commit``.

    Used when a merge succeeded but a later gate failed, so the branch never
    keeps work the gate rejected. Safe only because this branch and worktree
    belong to Cortex alone; it refuses to run anywhere else.
    """
    current = gitutil.branch(workspace.path)
    if current != BRANCH:
        raise IntegrationError(
            f"refusing to reset '{current}'; only '{BRANCH}' is Cortex's to rewind"
        )
    proc = _git(workspace.path, "reset", "--hard", commit)
    if proc.returncode != 0:
        raise IntegrationError(_message(proc, "git reset failed"))


def merged_branches(workspace: Workspace) -> list[str]:
    proc = _git(workspace.path, "branch", "--merged", BRANCH, "--format=%(refname:short)")
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def status(repo: str | Path, project_id: str) -> dict[str, object]:
    """What the owner needs to decide whether to merge integration onward."""
    repo = Path(repo)
    if not gitutil.is_repo(repo):
        return {"exists": False, "reason": "not a Git repository"}
    exists = _git(repo, "show-ref", "--verify", f"refs/heads/{BRANCH}").returncode == 0
    workspace_path = config.work_root() / project_id / WORKSPACE_DIR
    info: dict[str, object] = {
        "exists": exists,
        "branch": BRANCH,
        "workspace": str(workspace_path),
        "workspace_ready": workspace_path.exists() and gitutil.is_repo(workspace_path),
    }
    if not exists:
        return info
    try:
        base = base_branch(repo)
    except IntegrationError as exc:
        info["reason"] = str(exc)
        return info
    info["base"] = base
    info["ahead"] = _count(repo, f"{base}..{BRANCH}")
    info["behind"] = _count(repo, f"{BRANCH}..{base}")
    info["merges"] = _merge_log(repo)
    return info


def _merge_log(repo: Path, limit: int = 10) -> list[dict[str, str]]:
    proc = _git(
        repo, "log", BRANCH, "--merges", f"-{limit}",
        "--format=%H%x1f%an%x1f%aI%x1f%s",
    )
    if proc.returncode != 0:
        return []
    entries = []
    for line in proc.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            entries.append(
                {"commit": parts[0], "author": parts[1], "at": parts[2], "subject": parts[3]}
            )
    return entries


def _count(repo: Path, rev_range: str) -> int | None:
    proc = _git(repo, "rev-list", "--count", rev_range)
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:  # pragma: no cover - git prints an int or fails
        return None


def _conflicted_paths(repo: Path) -> list[str]:
    proc = _git(repo, "diff", "--name-only", "--diff-filter=U")
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _message(proc: subprocess.CompletedProcess[str], fallback: str) -> str:
    return (proc.stderr.strip() or proc.stdout.strip() or fallback)[:2000]


def _git(repo: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise IntegrationError(str(exc)) from exc
