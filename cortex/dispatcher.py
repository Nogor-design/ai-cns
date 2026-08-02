"""Task dispatch with dry-run defaults and isolated write worktrees."""

from __future__ import annotations

import fnmatch
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import brief as brief_mod
from . import (
    config, evidence, gitutil, ids, policy, routing, runlog, runs, secrets_scan,
    store, workers, worktrees,
)


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
    # A caller-supplied worker is a live instruction and is honoured verbatim:
    # if it is not permitted, dispatch says so rather than running something
    # else. Otherwise the effective route applies -- stored assignee first,
    # then project policy -- so this agrees with what the dashboard predicted.
    if worker_override:
        route = _with_overrides(
            routing.route_task(project, task),
            worker_override, model_override, action_override,
        )
    else:
        route = _with_overrides(
            routing.effective_route(project, task),
            None, model_override, action_override,
        )
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
        # Ollama cannot read the repository itself, so the relevant files are
        # inlined into its brief; the CLI workers open the workspace directly.
        compiled_text += "\n" + evidence.bundle(
            project["repo_path"], patterns_text=task["allowed_paths"]
        )
    # Scan whatever is about to be handed to a worker, local or not. Previously
    # only the Ollama brief was scanned, which meant the text most likely to
    # leave the machine was the one text never checked.
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
            effort=route.effort,
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
    on_run_start: Callable[[str], None] | None = None,
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
    # Privacy is enforced in exactly one place: the project's worker allowlist.
    # Risk no longer blocks read-only work, because a review that cannot run is
    # not safer than one that can -- it is just invisible. Blast radius is
    # governed separately by allow_write below.
    if route.blocked_reason:
        raise DispatchError(route.blocked_reason)
    if not policy.is_allowed(project, route.worker):
        raise DispatchError(
            f"{route.worker} is not on {project['name']}'s allowlist "
            f"(allowed: {', '.join(policy.allowed_workers(project))})"
        )

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
        effort=route.effort,
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
        effort=route.effort,
    )
    if on_run_start:
        on_run_start(run_id)
    runlog.start(
        run_id,
        header=(
            f"# {route.worker}:{route.model} ({route.effort or 'auto'} effort)\n"
            f"# {task['title']}\n"
            f"# workspace: {workspace}\n"
            f"# started: {ids.now()}\n\n"
        ),
    )
    try:
        result = workers.execute(
            command, workspace=workspace, brief=planned.brief, timeout=timeout,
            on_output=lambda text: runlog.append(run_id, text),
        )
    except workers.WorkerError as exc:
        runlog.append(run_id, f"\n[cortex] run failed: {exc}\n")
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
        # The readable answer; the full envelope stays in the run log.
        response=workers.extract_text(result.stdout),
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
    runlog.append(
        run_id,
        f"\n[cortex] finished with exit code {result.exit_code}; "
        f"task is now '{task_status}'.\n"
        + (f"[cortex] files changed: {', '.join(changed)}\n" if changed else "")
        + (f"[cortex] path-scope violations: {', '.join(violations)}\n" if violations else ""),
    )
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
        previous_worker = values["worker"]
        values["worker"] = worker
        if not model and previous_worker != worker:
            values["model"] = routing.DEFAULT_MODELS.get(worker, values["model"])
    if model:
        values["model"] = model
    if action:
        if action not in {"review", "research", "draft", "implement"}:
            raise DispatchError(f"invalid action override: {action}")
        values["action"] = action
    return routing.Route(**values)
