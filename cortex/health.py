"""Portfolio health inspection for registered Cortex projects."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, gitutil, store


@dataclass(frozen=True)
class ProjectHealth:
    project_id: str
    name: str
    program: str
    priority: int
    status: str
    privacy: str
    state_mode: str
    repo_path: str
    repo_exists: bool
    is_git: bool
    git_status_available: bool
    git_status_note: str | None
    branch: str | None
    modified: int
    untracked: int
    ahead: int | None
    behind: int | None
    last_commit_date: str | None
    last_commit_sha: str | None
    state_exists: bool
    state_age_days: int | None
    open_tasks: int
    assigned_tasks: int
    running_tasks: int
    review_tasks: int
    blocked_tasks: int
    health: str
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def inspect_project(
    conn: sqlite3.Connection,
    project: sqlite3.Row | dict[str, object],
    *,
    stale_days: int = 14,
    tasks: list[sqlite3.Row] | None = None,
    live_git: bool = True,
    scan_activity: bool = True,
) -> ProjectHealth:
    repo = Path(project["repo_path"])
    exists = repo.exists()
    snapshot = (
        gitutil.dashboard_snapshot(repo)
        if exists and project["status"] == "active" and live_git
        else gitutil.filesystem_snapshot(
            repo,
            note=(
                "Live Git status deferred for fast dashboard"
                if project["status"] == "active"
                else "Live Git status deferred while paused"
            ),
            scan_tree=scan_activity and project["status"] == "active",
        )
        if exists
        else gitutil.RepoSnapshot()
    )
    is_git = snapshot.is_git
    summary = snapshot.summary
    counts = (
        (snapshot.ahead, snapshot.behind)
        if snapshot.ahead is not None and snapshot.behind is not None
        else None
    )
    commit = snapshot.last_commit

    state_path = config.state_path(repo)
    state_exists = state_path.exists()
    state_age = _age_days(state_path) if state_exists else None

    tasks = tasks if tasks is not None else store.list_tasks(conn, str(project["id"]))
    task_counts = {
        status: sum(1 for task in tasks if task["status"] == status)
        for status in (
            "open", "assigned", "in_progress", "running", "review", "blocked"
        )
    }

    warnings: list[str] = []
    if not exists:
        warnings.append("repository path is missing")
    elif not is_git:
        warnings.append("not under Git")
    elif not summary.available and live_git and project["status"] == "active":
        warnings.append(snapshot.status_note or "Git status unavailable or timed out")
    if summary.dirty:
        warnings.append(
            f"dirty worktree: {summary.modified} modified, {summary.untracked} untracked"
        )
    if counts and counts[1] > 0:
        warnings.append(f"behind upstream by {counts[1]} commit(s)")
    if not state_exists and project["state_mode"] == "tracked":
        warnings.append("missing .cortex/state.md")
    elif state_age is not None and state_age > stale_days:
        warnings.append(f"state is stale ({state_age} days)")
    if task_counts["blocked"]:
        warnings.append(f"{task_counts['blocked']} blocked task(s)")

    if not exists:
        label = "blocked"
    elif warnings:
        label = "attention"
    else:
        label = "clean"

    return ProjectHealth(
        project_id=project["id"],
        name=project["name"],
        program=project["program"],
        priority=project["priority"],
        status=project["status"],
        privacy=project["privacy"],
        state_mode=project["state_mode"],
        repo_path=str(repo),
        repo_exists=exists,
        is_git=is_git,
        git_status_available=summary.available,
        git_status_note=snapshot.status_note,
        branch=snapshot.branch,
        modified=summary.modified,
        untracked=summary.untracked,
        ahead=counts[0] if counts else None,
        behind=counts[1] if counts else None,
        last_commit_date=commit[0] if commit else None,
        last_commit_sha=commit[1] if commit else None,
        state_exists=state_exists,
        state_age_days=state_age,
        open_tasks=task_counts["open"],
        assigned_tasks=task_counts["assigned"],
        running_tasks=task_counts["in_progress"] + task_counts["running"],
        review_tasks=task_counts["review"],
        blocked_tasks=task_counts["blocked"],
        health=label,
        warnings=tuple(warnings),
    )


def inspect_portfolio(
    conn: sqlite3.Connection,
    *,
    include_paused: bool = False,
    include_archived: bool = False,
    stale_days: int = 14,
    live_git: bool = True,
    scan_activity: bool = True,
) -> list[ProjectHealth]:
    projects = store.list_projects(conn)
    if not include_paused:
        projects = [project for project in projects if project["status"] == "active"]
    elif not include_archived:
        projects = [project for project in projects if project["status"] != "archived"]
    task_index: dict[str, list[sqlite3.Row]] = {project["id"]: [] for project in projects}
    for task in store.list_all_tasks(conn, include_done=False):
        if task["project_id"] in task_index:
            task_index[task["project_id"]].append(task)
    # Git inspection launches several independent subprocesses per repository.
    # A small pool keeps a many-project dashboard responsive without sharing
    # SQLite work across threads.
    project_dicts = [{key: project[key] for key in project.keys()} for project in projects]
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(project_dicts)))) as pool:
        rows = list(
            pool.map(
                lambda project: inspect_project(
                    conn,
                    project,
                    stale_days=stale_days,
                    tasks=task_index[str(project["id"])],
                    live_git=live_git,
                    scan_activity=scan_activity,
                ),
                project_dicts,
            )
        )
    return sorted(rows, key=lambda item: (item.priority, item.program, item.name.lower()))


def render_digest(rows: list[ProjectHealth]) -> str:
    """Render the concise daily digest as portable Markdown."""
    review = [row for row in rows if row.review_tasks]
    running = [row for row in rows if row.running_tasks]
    attention = [row for row in rows if row.warnings]
    clean = len([row for row in rows if row.health == "clean"])

    lines = ["# Cortex daily digest", ""]
    _append_section(
        lines,
        "Ready for review",
        [f"{row.project_id}: {row.review_tasks} task(s)" for row in review],
    )
    _append_section(
        lines,
        "Running",
        [f"{row.project_id}: {row.running_tasks} task(s)" for row in running],
    )
    _append_section(
        lines,
        "Needs attention",
        [f"{row.project_id}: {'; '.join(row.warnings)}" for row in attention],
    )
    lines.extend(
        [
            "## Summary",
            "",
            f"{len(rows)} active, {clean} clean, {len(attention)} need attention.",
            "",
        ]
    )
    return "\n".join(lines)


def _append_section(lines: list[str], title: str, items: list[str]) -> None:
    lines.extend([f"## {title}", ""])
    if not items:
        lines.append("- None")
    else:
        lines.extend(f"- {item}" for item in items)
    lines.append("")


def _age_days(path: Path) -> int:
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return max(0, (datetime.now(tz=timezone.utc) - modified).days)
