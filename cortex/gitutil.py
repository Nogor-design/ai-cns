"""Git provenance helpers via subprocess (spec section 10).

All functions are tolerant of non-git directories and missing git, returning
None / empty rather than raising, so the rest of the system degrades gracefully.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StatusSummary:
    modified: int = 0
    untracked: int = 0

    @property
    def dirty(self) -> int:
        return self.modified + self.untracked


def _run(repo_path: str | Path, *args: str) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            timeout=60,
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
    code, out, _ = _run(repo_path, "status", "--porcelain=v1")
    if code != 0:
        return StatusSummary()
    lines = [line for line in out.splitlines() if line]
    return StatusSummary(
        modified=sum(1 for line in lines if not line.startswith("??")),
        untracked=sum(1 for line in lines if line.startswith("??")),
    )


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
    code, out, _ = _run(repo_path, "log", "-1", "--format=%cs%x00%h%x00%s")
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
