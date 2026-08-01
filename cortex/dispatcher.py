"""Task dispatch with dry-run defaults and isolated write worktrees."""

from __future__ import annotations

import fnmatch
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import brief as brief_mod
from . import config, evidence, gitutil, ids, routing, runs, secrets_scan, store, workers, worktrees


class DispatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class DispatchPreview:
    route: routing.Route
    command: workers.CommandSpec | None
    workspace: Path
    brief: str
    note: str | None = None


@dataclass(frozen=True)
class DispatchResult:
    run_id: str
    task_status: str
    worker: str
    model: str
    workspace: Path
    exit_code: int
    files_changed: tuple[str, ...]
    tests_passed: int | None
    violations: tuple[str, ...]


def preview(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    worker_override: str | None = None,
    model_override: str | None = None,
    action_override: str | None = None,
) -> DispatchPreview:
    project = store.get_project(conn, task["project_id"])
    route = routing.route_task(project, task)
    route = _with_overrides(route, worker_override, model_override, action_override)
    compiled = brief_mod.compile_brief(
        conn,
        project,
        task=task,
        model=route.model,
        mode="manual",
        use_ollama=False,
    )
    compiled_text = compiled.text
    if route.worker == "ollama":
        compiled_text += "\n" + evidence.bundle(
            project["repo_path"], patterns_text=task["allowed_paths"]
        )
        findings = secrets_scan.scan(compiled_text, use_ollama=False)
        if findings:
            raise brief_mod.SecretsDetected(findings)

    workspace = Path(project["repo_path"])
    brief_path = config.run_root() / task["id"] / "brief.md"
    try:
        command = workers.build_command(
            worker=route.worker,
            model=route.model,
            action=route.action,
            workspace=workspace,
            brief_path=brief_path,
            budget=route.budget,
        )
        note = None
    except workers.WorkerError as exc:
        command = None
        note = str(exc)
    return DispatchPreview(route, command, workspace, compiled_text, note)


def dispatch(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    worker_override: str | None = None,
    model_override: str | None = None,
    action_override: str | None = None,
    allow_write: bool = False,
    approve_high_risk: bool = False,
    timeout: int = 1800,
) -> DispatchResult:
    project = store.get_project(conn, task["project_id"])
    planned = preview(
        conn,
        task,
        worker_override=worker_override,
        model_override=model_override,
        action_override=action_override,
    )
    route = planned.route
    if planned.command is None:
        raise DispatchError(planned.note or "no executable worker command")
    if route.requires_approval and not approve_high_risk:
        raise DispatchError("high-risk route requires --approve-high-risk")
    if project["privacy"] == "restricted" and route.worker != "ollama" and not approve_high_risk:
        raise DispatchError("restricted project requires local Ollama or explicit approval")

    write = route.action == "implement"
    workspace = Path(project["repo_path"])
    if write:
        if not allow_write:
            raise DispatchError("implementation route requires --allow-write")
        try:
            workspace = worktrees.ensure(
                project["repo_path"], project["id"], task["id"]
            ).path
        except worktrees.WorktreeError as exc:
            raise DispatchError(str(exc)) from exc

    run_dir = config.run_root() / task["id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    brief_path = run_dir / "brief.md"
    brief_path.write_text(planned.brief, encoding="utf-8")
    command = workers.build_command(
        worker=route.worker,
        model=route.model,
        action=route.action,
        workspace=workspace,
        brief_path=brief_path,
        budget=route.budget,
    )

    before = gitutil.head(workspace) if gitutil.is_repo(workspace) else None
    store.update_task(
        conn,
        task["id"],
        status="running",
        execution_mode="headless_cli",
        model=f"{route.worker}:{route.model}",
        brief=planned.brief,
    )
    run_id = store.create_run(
        conn,
        task_id=task["id"],
        project_id=project["id"],
        model=f"{route.worker}:{route.model}",
        execution_mode="headless_cli",
        git_before=before,
        captured_via="cli",
    )
    try:
        result = workers.execute(
            command, workspace=workspace, brief=planned.brief, timeout=timeout
        )
    except workers.WorkerError as exc:
        store.update_run(
            conn,
            run_id,
            ended_at=ids.now(),
            exit_code=1,
            human_note=str(exc),
            workspace_path=str(workspace),
            command_json=json.dumps(command.argv),
        )
        store.update_task(conn, task["id"], status="blocked")
        raise DispatchError(str(exc)) from exc

    changed = gitutil.changed_files(workspace, before) if write else []
    size = gitutil.diff_size(workspace, before) if write else 0
    violations = _path_violations(changed, task["allowed_paths"]) if write else []
    tests_passed: int | None = None
    if write and project["test_command"]:
        tests_passed = runs.execute_test_command(workspace, project["test_command"])

    failed = result.exit_code != 0 or bool(violations) or tests_passed == 0
    task_status = (
        "blocked"
        if failed
        else "review"
        if write or route.reviewer
        else "done"
    )
    store.update_run(
        conn,
        run_id,
        ended_at=ids.now(),
        git_after=gitutil.head(workspace) if write else before,
        files_changed=changed,
        diff_size=size,
        tests_passed=tests_passed,
        response=result.stdout,
        human_note=(
            result.stderr[-4000:]
            if result.stderr and result.exit_code != 0
            else None
        ),
        workspace_path=str(workspace),
        command_json=json.dumps(command.argv),
        usage_json=json.dumps(result.usage) if result.usage else None,
        exit_code=result.exit_code,
    )
    store.update_task(conn, task["id"], status=task_status)
    return DispatchResult(
        run_id=run_id,
        task_status=task_status,
        worker=route.worker,
        model=route.model,
        workspace=workspace,
        exit_code=result.exit_code,
        files_changed=tuple(changed),
        tests_passed=tests_passed,
        violations=tuple(violations),
    )


def _path_violations(changed: list[str], allowed: str | None) -> list[str]:
    if not allowed:
        return []
    try:
        parsed = json.loads(allowed)
        patterns = parsed if isinstance(parsed, list) else [str(parsed)]
    except json.JSONDecodeError:
        patterns = [part.strip() for part in allowed.replace(",", "\n").splitlines()]
    patterns = [pattern for pattern in patterns if pattern]
    return [
        path
        for path in changed
        if not any(fnmatch.fnmatch(path, pattern) for pattern in patterns)
    ]


def _with_overrides(
    route: routing.Route,
    worker: str | None,
    model: str | None,
    action: str | None,
) -> routing.Route:
    values = route.__dict__.copy()
    if worker:
        values["worker"] = worker
    if model:
        values["model"] = model
    if action:
        if action not in {"review", "research", "draft", "implement"}:
            raise DispatchError(f"invalid action override: {action}")
        values["action"] = action
    return routing.Route(**values)
