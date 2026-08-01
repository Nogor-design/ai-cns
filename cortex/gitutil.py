"""Git provenance helpers via subprocess (spec section 10).

All functions are tolerant of non-git directories and missing git, returning
None / empty rather than raising, so the rest of the system degrades gracefully.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class StatusSummary:
    modified: int = 0
    untracked: int = 0
    available: bool = True

    @property
    def dirty(self) -> int:
        return self.modified + self.untracked


@dataclass(frozen=True)
class RepoSnapshot:
    is_git: bool = False
    branch: str | None = None
    summary: StatusSummary = StatusSummary(available=False)
    ahead: int | None = None
    behind: int | None = None
    last_commit: tuple[str, str, str] | None = None
    status_note: str | None = None


def _run(
    repo_path: str | Path, *args: str, timeout: float = 60
) -> tuple[int, str, str]:
    try:
        env = os.environ.copy()
        # Background portfolio reads should never contend for Git's optional
        # index refresh lock with the owner's IDE or another agent.
        env["GIT_OPTIONAL_LOCKS"] = "0"
        proc = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
        return 1, "", str(exc)


def is_repo(repo_path: str | Path) -> bool:
    code, out, _ = _run(repo_path, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


def branch(repo_path: str | Path) -> str | None:
    code, out, _ = _run(repo_path, "branch", "--show-current")
    if code != 0:
        return None
    return out.strip() or "(detached)"


def status_summary(repo_path: str | Path) -> StatusSummary:
    # ``-- .`` scopes logical projects that live inside a shared monorepo while
    # remaining equivalent to a normal status call at a repository root.
    code, out, _ = _run(repo_path, "status", "--porcelain=v1", "--", ".")
    if code != 0:
        return StatusSummary(available=False)
    lines = [line for line in out.splitlines() if line]
    return StatusSummary(
        modified=sum(1 for line in lines if not line.startswith("??")),
        untracked=sum(1 for line in lines if line.startswith("??")),
    )


def dashboard_snapshot(repo_path: str | Path, *, timeout: float = 2) -> RepoSnapshot:
    """Bounded Git evidence for an interactive dashboard refresh.

    Porcelain v2 returns branch/upstream and scoped worktree state in one call.
    A separate scoped log supplies last activity. Both calls have short timeouts
    so a locked or unhealthy repository cannot stall the whole portfolio.
    """
    repo = Path(repo_path)
    root = _find_git_root(repo)
    if root is None:
        return RepoSnapshot()
    # Logical projects inside a monorepo share one index. Running many scoped
    # status commands concurrently can contend badly on Windows, so use
    # filesystem evidence and the shared HEAD for these rows.
    if root.resolve() != repo.resolve():
        return filesystem_snapshot(
            repo,
            note="Scoped Git status deferred for shared repository",
            scan_tree=True,
        )

    code, out, _ = _run(
        repo_path,
        "status",
        "--porcelain=v2",
        "--branch",
        "--untracked-files=normal",
        "--",
        ".",
        timeout=timeout,
    )
    if code != 0:
        return filesystem_snapshot(
            repo,
            note="Git status unavailable or timed out",
        )

    branch_name: str | None = None
    ahead: int | None = None
    behind: int | None = None
    modified = 0
    untracked = 0
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            branch_name = line.removeprefix("# branch.head ").strip()
        elif line.startswith("# branch.ab "):
            parts = line.removeprefix("# branch.ab ").split()
            if len(parts) == 2:
                ahead = int(parts[0].lstrip("+"))
                behind = int(parts[1].lstrip("-"))
        elif line.startswith("? "):
            untracked += 1
        elif line and not line.startswith("#"):
            modified += 1

    code, commit_out, _ = _run(
        repo_path,
        "log",
        "-1",
        "--format=%cs%x00%h%x00%s",
        "--",
        ".",
        timeout=timeout,
    )
    commit_parts = commit_out.strip().split("\x00", 2) if code == 0 else []
    commit = tuple(commit_parts) if len(commit_parts) == 3 else None
    return RepoSnapshot(
        is_git=True,
        branch=branch_name or "(detached)",
        summary=StatusSummary(modified=modified, untracked=untracked),
        ahead=ahead,
        behind=behind,
        last_commit=commit,
    )


def filesystem_snapshot(
    repo_path: str | Path,
    *,
    note: str = "Live Git status deferred",
    scan_tree: bool = False,
) -> RepoSnapshot:
    """Fast, subprocess-free fallback for paused or shared-repository rows."""
    repo = Path(repo_path)
    root = _find_git_root(repo)
    if root is None:
        return RepoSnapshot()
    branch_name = _read_head_branch(root)
    latest: float | None = None
    ignored = {".git", ".pytest_cache", "__pycache__", "node_modules", ".venv"}
    try:
        candidates: list[Path] = [repo]
        if scan_tree:
            # Prune heavyweight generated folders before descending; cap the
            # scan so last-activity evidence can never dominate a refresh.
            seen = 0
            for current, dirs, files in os.walk(repo):
                dirs[:] = [name for name in dirs if name not in ignored]
                for name in files:
                    candidates.append(Path(current) / name)
                    seen += 1
                    if seen >= 5000:
                        dirs[:] = []
                        break
                if seen >= 5000:
                    break
        else:
            candidates.extend(path for path in repo.iterdir() if path.is_file())
        for path in candidates:
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            latest = modified if latest is None else max(latest, modified)
    except OSError:
        pass
    commit = None
    if latest is not None:
        date = datetime.fromtimestamp(latest, tz=timezone.utc).date().isoformat()
        commit = (date, "filesystem", "Latest scoped file activity")
    return RepoSnapshot(
        is_git=True,
        branch=branch_name,
        summary=StatusSummary(available=False),
        last_commit=commit,
        status_note=note,
    )


def _find_git_root(path: Path) -> Path | None:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _read_head_branch(root: Path) -> str | None:
    git_marker = root / ".git"
    git_dir = git_marker
    if git_marker.is_file():
        try:
            marker = git_marker.read_text(encoding="utf-8", errors="replace").strip()
            if marker.startswith("gitdir:"):
                git_dir = (root / marker.split(":", 1)[1].strip()).resolve()
        except OSError:
            return None
    try:
        head_text = (git_dir / "HEAD").read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    except OSError:
        return None
    if head_text.startswith("ref: refs/heads/"):
        return head_text.removeprefix("ref: refs/heads/")
    return "(detached)" if head_text else None


def upstream_counts(repo_path: str | Path) -> tuple[int, int] | None:
    """Return (ahead, behind) relative to the configured upstream."""
    code, upstream, _ = _run(
        repo_path, "rev-parse", "--abbrev-ref", "@{upstream}"
    )
    if code != 0 or not upstream.strip():
        return None
    code, out, _ = _run(
        repo_path,
        "rev-list",
        "--left-right",
        "--count",
        f"{upstream.strip()}...HEAD",
    )
    if code != 0:
        return None
    parts = out.split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    behind, ahead = (int(part) for part in parts)
    return ahead, behind


def last_commit(repo_path: str | Path) -> tuple[str, str, str] | None:
    """Return (date, short sha, subject) for the latest commit."""
    code, out, _ = _run(
        repo_path, "log", "-1", "--format=%cs%x00%h%x00%s", "--", "."
    )
    if code != 0:
        return None
    parts = out.strip().split("\x00", 2)
    return tuple(parts) if len(parts) == 3 else None


def head(repo_path: str | Path) -> str | None:
    """Current HEAD sha, or None if no commits / not a repo."""
    code, out, _ = _run(repo_path, "rev-parse", "HEAD")
    if code != 0:
        return None
    return out.strip() or None


def changed_files(repo_path: str | Path, before: str | None) -> list[str]:
    """Files changed since `before` (committed or in the working tree).

    Combines committed changes (before..HEAD) with current working-tree
    changes (tracked + untracked) so agentic edits are captured whether or
    not the agent committed.
    """
    files: set[str] = set()
    if before:
        code, out, _ = _run(repo_path, "diff", "--name-only", f"{before}..HEAD")
        if code == 0:
            files.update(f for f in out.splitlines() if f.strip())
    # Working-tree changes (tracked) relative to HEAD.
    code, out, _ = _run(repo_path, "diff", "--name-only", "HEAD")
    if code == 0:
        files.update(f for f in out.splitlines() if f.strip())
    # Untracked files.
    code, out, _ = _run(repo_path, "ls-files", "--others", "--exclude-standard")
    if code == 0:
        files.update(f for f in out.splitlines() if f.strip())
    return sorted(files)


def diff_size(repo_path: str | Path, before: str | None) -> int:
    """Total lines changed (added + deleted) since `before`, a cheap risk signal."""
    total = 0
    rng = f"{before}..HEAD" if before else "HEAD"
    code, out, _ = _run(repo_path, "diff", "--numstat", rng)
    if code == 0:
        total += _sum_numstat(out)
    # Include uncommitted working-tree changes to tracked files.
    code, out, _ = _run(repo_path, "diff", "--numstat", "HEAD")
    if code == 0:
        total += _sum_numstat(out)
    # Untracked files are not in `git diff`; count their lines directly so new
    # files still register on the cheap risk signal.
    total += _untracked_lines(repo_path)
    return total


def _untracked_lines(repo_path: str | Path) -> int:
    code, out, _ = _run(repo_path, "ls-files", "--others", "--exclude-standard")
    if code != 0:
        return 0
    base = Path(repo_path)
    total = 0
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        fp = base / rel
        try:
            with fp.open("rb") as fh:
                total += sum(1 for _ in fh)
        except OSError:
            continue
    return total


def _sum_numstat(out: str) -> int:
    total = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            for n in parts[:2]:
                if n.isdigit():
                    total += int(n)
    return total


def is_ancestor(repo_path: str | Path, ancestor: str, descendant: str) -> bool:
    """True if `ancestor` commit is reachable from `descendant`."""
    code, _, _ = _run(
        repo_path, "merge-base", "--is-ancestor", ancestor, descendant
    )
    return code == 0


def commits_between(repo_path: str | Path, before: str, after: str = "HEAD") -> int:
    code, out, _ = _run(repo_path, "rev-list", "--count", f"{before}..{after}")
    if code != 0:
        return 0
    out = out.strip()
    return int(out) if out.isdigit() else 0
