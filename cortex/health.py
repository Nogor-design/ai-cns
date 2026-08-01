"""Portfolio health inspection for registered Cortex projects."""

from __future__ import annotations

import sqlite3
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
    repo_path: str
    repo_exists: bool
    is_git: bool
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
    running_tasks: int
    review_tasks: int
    blocked_tasks: int
    health: str
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def inspect_project(
    conn: sqlite3.Connection, project: sqlite3.Row, *, stale_days: int = 14
) -> ProjectHealth:
    repo = Path(project["repo_path"])
    exists = repo.exists()
    is_git = exists and gitutil.is_repo(repo)
    summary = gitutil.status_summary(repo) if is_git else gitutil.StatusSummary()
    counts = gitutil.upstream_counts(repo) if is_git else None
    commit = gitutil.last_commit(repo) if is_git else None

    state_path = config.state_path(repo)
    state_exists = state_path.exists()
    state_age = _age_days(state_path) if state_exists else None

    tasks = store.list_tasks(conn, project["id"])
    task_counts = {
        status: sum(1 for task in tasks if task["status"] == status)
        for status in ("open", "in_progress", "running", "review", "blocked")
    }

    warnings: list[str] = []
    if not exists:
        warnings.append("repository path is missing")
    elif not is_git:
        warnings.append("not under Git")
    if summary.dirty:
        warnings.append(
            f"dirty worktree: {summary.modified} modified, {summary.untracked} untracked"
        )
    if counts and counts[1] > 0:
        warnings.append(f"behind upstream by {counts[1]} commit(s)")
    if not state_exists:
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
        repo_path=str(repo),
        repo_exists=exists,
        is_git=is_git,
        branch=gitutil.branch(repo) if is_git else None,
        modified=summary.modified,
        untracked=summary.untracked,
        ahead=counts[0] if counts else None,
        behind=counts[1] if counts else None,
        last_commit_date=commit[0] if commit else None,
        last_commit_sha=commit[1] if commit else None,
        state_exists=state_exists,
        state_age_days=state_age,
        open_tasks=task_counts["open"],
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
    stale_days: int = 14,
) -> list[ProjectHealth]:
    projects = store.list_projects(conn)
    if not include_paused:
        projects = [project for project in projects if project["status"] == "active"]
    rows = [inspect_project(conn, project, stale_days=stale_days) for project in projects]
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
