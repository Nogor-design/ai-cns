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


def ensure(repo: str | Path, project_id: str, task_id: str) -> Worktree:
    repo = Path(repo)
    if not gitutil.is_repo(repo):
        raise WorktreeError(f"not a Git repository: {repo}")
    dirty = gitutil.status_summary(repo)
    if dirty.dirty:
        raise WorktreeError(
            "canonical repository is dirty; checkpoint or commit it before a write run "
            f"({dirty.modified} modified, {dirty.untracked} untracked)"
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
    args.extend([str(target), branch if branch_exists else "HEAD"])
    proc = _git(repo, *args)
    if proc.returncode != 0:
        raise WorktreeError(proc.stderr.strip() or proc.stdout.strip() or "git worktree add failed")
    return Worktree(path=target, branch=branch, created=True)


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
