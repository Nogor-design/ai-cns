"""Safe Git worktree creation for write-capable agent runs."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config, gitutil


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    created: bool


def ensure(
    repo: str | Path, project_id: str, task_id: str, *, base: str | None = None
) -> Worktree:
    """A private branch and checkout for one task's write run.

    ``base`` is the revision the branch starts from. Phase 3 passes the
    project's integration branch, so a write run is built on top of work that
    has already been verified and merged rather than on the owner's ``main``.
    """
    repo = Path(repo)
    if not gitutil.is_repo(repo):
        raise WorktreeError(f"not a Git repository: {repo}")
    # The owner's uncommitted work is a real reason to refuse; Cortex's own
    # bookkeeping under .cortex/ is not. A scaffolded state.md that the project
    # does not track would otherwise block every write run forever.
    dirty = [
        path for path in gitutil.dirty_paths(repo)
        if not path.startswith(f"{config.CORTEX_DIR}/")
    ]
    if dirty:
        raise WorktreeError(
            "canonical repository has uncommitted changes; commit or stash them "
            f"before a write run ({len(dirty)}: {', '.join(dirty[:5])})"
        )

    branch = f"cortex/{project_id}/{task_id}"
    target = config.work_root() / project_id / task_id
    if target.exists():
        if gitutil.is_repo(target):
            return Worktree(path=target, branch=branch, created=False)
        raise WorktreeError(f"worktree target exists but is not a repository: {target}")

    target.parent.mkdir(parents=True, exist_ok=True)
    branch_exists = _git(repo, "show-ref", "--verify", f"refs/heads/{branch}").returncode == 0
    args = ["worktree", "add"]
    if not branch_exists:
        args.extend(["-b", branch])
    args.extend([str(target), branch if branch_exists else (base or "HEAD")])
    proc = _git(repo, *args)
    if proc.returncode != 0:
        raise WorktreeError(proc.stderr.strip() or proc.stdout.strip() or "git worktree add failed")
    return Worktree(path=target, branch=branch, created=True)


def commit_work(
    worktree: str | Path,
    message: str,
    *,
    author: str = "Cortex agent <cortex@localhost>",
) -> str | None:
    """Commit whatever an agent left behind, returning the commit or None.

    Agents differ: some commit their own work, some leave edited and untracked
    files in the worktree. A verification gate can only judge, merge and revert
    a commit, so anything still loose is committed here on the task's own
    branch. Nothing outside this private worktree is touched.
    """
    path = Path(worktree)
    if not gitutil.is_repo(path):
        raise WorktreeError(f"not a Git repository: {path}")
    if gitutil.status_summary(path).dirty == 0:
        return None
    staged = _git(path, "add", "-A")
    if staged.returncode != 0:
        raise WorktreeError(staged.stderr.strip() or "git add failed")
    name, _, email = author.partition(" <")
    proc = _git(
        path,
        "-c", f"user.name={name}", "-c", f"user.email={email.rstrip('>') or 'cortex@localhost'}",
        "commit", "-q", "-m", message,
    )
    if proc.returncode != 0:
        raise WorktreeError(proc.stderr.strip() or proc.stdout.strip() or "git commit failed")
    return gitutil.head(path)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(str(exc)) from exc
