"""Persistent, bounded Git/GitHub synchronization checks for the portfolio."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import gitutil, ids, store


def refresh_portfolio(
    conn: sqlite3.Connection,
    *,
    fetch: bool = False,
    include_paused: bool = False,
    max_workers: int = 4,
) -> list[dict[str, Any]]:
    projects = [
        row for row in store.list_projects(conn)
        if row["status"] != "archived"
        and (include_paused or row["status"] == "active")
    ]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_inspect, row["id"], row["repo_path"], fetch): row["id"]
            for row in projects
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({
                    "project_id": futures[future], "is_git": False,
                    "note": str(exc), "checked_at": ids.now(), "fetched": False,
                })
    for result in results:
        project_id = result.pop("project_id")
        store.upsert_git_check(conn, project_id, **result)
        result["project_id"] = project_id
    return sorted(results, key=lambda row: row["project_id"])


def _inspect(project_id: str, repo_path: str, fetch: bool) -> dict[str, Any]:
    repo = Path(repo_path)
    fetch_note: str | None = None
    fetched = False
    if fetch and gitutil.is_repo(repo):
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo), "fetch", "--quiet", "--prune"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=25,
                env=env,
            )
            fetched = proc.returncode == 0
            if proc.returncode != 0:
                fetch_note = (proc.stderr or proc.stdout or "Git fetch failed")[-500:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            fetch_note = f"Git fetch unavailable: {exc}"
    snapshot = gitutil.dashboard_snapshot(repo, timeout=5)
    commit = snapshot.last_commit
    notes = [part for part in (snapshot.status_note, fetch_note) if part]
    return {
        "project_id": project_id,
        "is_git": snapshot.is_git,
        "branch": snapshot.branch,
        "modified": snapshot.summary.modified,
        "untracked": snapshot.summary.untracked,
        "ahead": snapshot.ahead,
        "behind": snapshot.behind,
        "last_commit_date": commit[0] if commit else None,
        "last_commit_sha": commit[1] if commit else None,
        "last_commit_subject": commit[2] if commit else None,
        "fetched": fetched,
        "note": "; ".join(notes) or None,
        "checked_at": ids.now(),
    }
