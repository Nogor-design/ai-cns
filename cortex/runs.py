"""The run loop — feature #2 (spec section 7).

One loop, not six:

    compile brief -> execute (agentic | api | manual) -> capture outcome -> update state

Each mode captures its outcome with the least possible user effort. Agentic
capture is fully automatic via git + tests; API capture stores the response;
manual is the degraded fallback.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import api_client, brief as brief_mod, gitutil, ids, store

ApiCaller = Callable[[str, str], str]


@dataclass
class StartedRun:
    run_id: str
    mode: str
    brief: brief_mod.CompiledBrief | None
    response: str | None = None
    note: str | None = None


def start_run(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    mode: str,
    model: str | None,
    use_ollama: bool = True,
    api_caller: ApiCaller | None = None,
) -> StartedRun:
    """Begin a run in the given mode and capture whatever is automatic."""
    project = store.get_project(conn, task["project_id"])

    if mode == "agentic_cli":
        return _start_agentic(conn, project, task, model, use_ollama)
    if mode == "api":
        return _start_api(conn, project, task, model, use_ollama, api_caller)
    if mode == "manual":
        return _start_manual(conn, project, task, model, use_ollama)
    raise ValueError(f"unknown mode: {mode}")


def _compile(conn, project, task, model, mode, use_ollama) -> brief_mod.CompiledBrief:
    compiled = brief_mod.compile_brief(
        conn, project, task=task, model=model, mode=mode, use_ollama=use_ollama
    )
    # Persist the brief and chosen mode/model onto the task for provenance.
    store.update_task(
        conn, task["id"], brief=compiled.text, execution_mode=mode, model=model,
        status="in_progress",
    )
    return compiled


def _start_agentic(conn, project, task, model, use_ollama) -> StartedRun:
    repo = project["repo_path"]
    git_before = gitutil.head(repo) if gitutil.is_repo(repo) else None
    compiled = _compile(conn, project, task, model, "agentic_cli", use_ollama)
    rid = store.create_run(
        conn,
        task_id=task["id"],
        project_id=project["id"],
        model=model,
        execution_mode="agentic_cli",
        git_before=git_before,
        captured_via="git",
    )
    note = (
        "Brief written to .cortex/last_brief.md. Run the agent in the repo, then "
        f"`cortex done {rid}` to capture the diff and test results."
    )
    return StartedRun(run_id=rid, mode="agentic_cli", brief=compiled, note=note)


def _start_api(conn, project, task, model, use_ollama, api_caller) -> StartedRun:
    compiled = _compile(conn, project, task, model, "api", use_ollama)
    rid = store.create_run(
        conn,
        task_id=task["id"],
        project_id=project["id"],
        model=model,
        execution_mode="api",
        captured_via="api",
    )
    caller = api_caller or api_client.call
    response = caller(model or "", compiled.text)
    store.update_run(conn, rid, response=response, ended_at=ids.now())
    store.update_task(conn, task["id"], status="done")
    return StartedRun(run_id=rid, mode="api", brief=compiled, response=response)


def _start_manual(conn, project, task, model, use_ollama) -> StartedRun:
    compiled = _compile(conn, project, task, model, "manual", use_ollama)
    rid = store.create_run(
        conn,
        task_id=task["id"],
        project_id=project["id"],
        model=model,
        execution_mode="manual",
        captured_via="manual",
    )
    note = (
        f"Brief ready. Paste it into {model or 'the model'}, then "
        f"`cortex capture {rid}` to record the response."
    )
    return StartedRun(run_id=rid, mode="manual", brief=compiled, note=note)


# ----------------------------------------------------------- close / capture ---
@dataclass
class DoneResult:
    run_id: str
    files_changed: list[str]
    diff_size: int
    tests_passed: int | None


def finish_agentic(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    *,
    run_tests: bool = True,
) -> DoneResult:
    """Capture the outcome of an agentic run via git diff + the test command."""
    project = store.get_project(conn, run["project_id"])
    repo = project["repo_path"]
    before = run["git_before"]

    files = gitutil.changed_files(repo, before) if gitutil.is_repo(repo) else []
    size = gitutil.diff_size(repo, before) if gitutil.is_repo(repo) else 0
    after = gitutil.head(repo) if gitutil.is_repo(repo) else None

    tests_passed: int | None = None
    if run_tests and project["test_command"]:
        tests_passed = execute_test_command(repo, project["test_command"])

    store.update_run(
        conn,
        run["id"],
        git_after=after,
        files_changed=files,
        diff_size=size,
        tests_passed=tests_passed,
        ended_at=ids.now(),
        outcome="unknown",
    )
    return DoneResult(
        run_id=run["id"], files_changed=files, diff_size=size, tests_passed=tests_passed
    )


def execute_test_command(repo: str | Path, command: str) -> int:
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(repo),
            capture_output=True, text=True, timeout=600,
        )
        return 1 if proc.returncode == 0 else 0
    except (subprocess.TimeoutExpired, OSError):
        return 0


def capture_manual(
    conn: sqlite3.Connection, run: sqlite3.Row, response: str, *, note: str | None = None
) -> None:
    """Record a pasted-back response for a manual run."""
    fields = {"response": response, "ended_at": ids.now()}
    if note:
        fields["human_note"] = note
    store.update_run(conn, run["id"], **fields)
    store.update_task(conn, run["task_id"], status="done")


# ---------------------------------------------------------- outcome resolve ---
def resolve_outcomes(conn: sqlite3.Connection, project: sqlite3.Row) -> dict[str, str]:
    """Resolve survived/reverted for closed agentic runs (spec section 7).

    A change `survived` if the commit it produced is an ancestor of the current
    HEAD (committed and not rewritten away); `reverted` if HEAD moved on but the
    run's commit is no longer reachable. Runs with no commit yet stay `unknown`.
    """
    repo = project["repo_path"]
    results: dict[str, str] = {}
    if not gitutil.is_repo(repo):
        return results
    current = gitutil.head(repo)
    for run in store.unresolved_runs(conn, project["id"]):
        before, after = run["git_before"], run["git_after"]
        outcome = "unknown"
        if after and after != before:
            # The agent committed. Did its commit survive into current history?
            outcome = "survived" if gitutil.is_ancestor(repo, after, current) else "reverted"
        else:
            # No commit at done time; check whether the work has since landed.
            if before and current and gitutil.commits_between(repo, before, current) > 0:
                outcome = "survived"
        if outcome != "unknown":
            store.update_run(conn, run["id"], outcome=outcome)
            results[run["id"]] = outcome
    return results


def files_changed_list(run: sqlite3.Row) -> list[str]:
    if not run["files_changed"]:
        return []
    try:
        return json.loads(run["files_changed"])
    except (json.JSONDecodeError, TypeError):
        return []
