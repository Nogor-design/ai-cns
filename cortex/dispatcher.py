"""Task dispatch with dry-run defaults and isolated write worktrees."""

from __future__ import annotations

import fnmatch
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from . import brief as brief_mod
from . import (
    autonomy, capacity, config, evidence, gitutil, ids, integration, lanes, policy,
    routing, model_catalog, runlog, runs, secrets_scan, project_blueprints, store,
    supervisor, trading_guard, verification, workers, worktrees,
)

SCHEDULER = "scheduler"


class DispatchError(RuntimeError):
    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        # Set when the failure happened after a run row existed.
        self.run_id = run_id


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
    # The Phase 3 gate's report for a write run; None for read-only work.
    verification: dict[str, object] | None = None


def preview(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    worker_override: str | None = None,
    model_override: str | None = None,
    action_override: str | None = None,
) -> DispatchPreview:
    project = store.get_project(conn, task["project_id"])
    try:
        project_blueprints.assert_execution_ready(project)
    except ValueError as exc:
        raise DispatchError(str(exc)) from exc
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
    route = _with_owner_defaults(conn, route, task, model_override, action_override)
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
    started_by: str = "owner",
    watchdog: Callable[[], str | None] | None = None,
    observer: Callable[[str], None] | None = None,
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
    admitted: dict[str, object] | None = None
    if started_by == SCHEDULER:
        # Checked here, at the last moment, as well as when candidates are
        # chosen: settings and quota can change between the two.
        refusal = unattended_refusal(conn, project, route, task)
        if refusal:
            raise DispatchError(refusal)
        admitted = admission_evidence(conn, route)
    workspace = Path(project["repo_path"])
    if write:
        if not allow_write:
            raise DispatchError("implementation route requires --allow-write")
        # Write work is branched from the project's integration branch, not from
        # the owner's main: an agent builds on what the gate has already
        # accepted, and its branch merges back into the same place.
        try:
            base = integration.base_for_task(project["repo_path"], project["id"]).branch
            workspace = worktrees.ensure(
                project["repo_path"], project["id"], task["id"], base=base
            ).path
        except (integration.IntegrationError, worktrees.WorktreeError) as exc:
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
    store.update_run(conn, run_id, started_by=started_by)
    if started_by == SCHEDULER:
        store.create_activity_event(
            conn,
            project_id=project["id"],
            task_id=task["id"],
            actor_type="system",
            actor_name="cortex-scheduler",
            model=f"{route.worker}:{route.model}",
            action="run.unattended_start",
            summary=f"Started unattended {route.action} with {route.worker}",
            source="cortex-scheduler",
            source_ref=run_id,
            evidence=admitted,
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
        supervised = {"watchdog": watchdog} if watchdog is not None else {}
        result = workers.execute(
            command, workspace=workspace, brief=planned.brief, timeout=timeout,
            on_output=lambda text: _observe(run_id, text, observer),
            **supervised,
        )
    except workers.WorkerStopped as exc:
        # Keep what the stopped run cost and any quota it reported.
        partial = workers.WorkerResult(
            exit_code=1, stdout=exc.stdout, stderr=exc.stderr, usage=exc.usage
        )
        _record_quota(conn, route.worker, partial, run_id)
        store.update_run(
            conn,
            run_id,
            ended_at=ids.now(),
            exit_code=1,
            outcome="unknown",
            response=workers.extract_text(exc.stdout) if exc.stdout else None,
            human_note=f"Stopped by the Cortex supervisor: {exc.reason}",
            workspace_path=str(workspace),
            command_json=json.dumps(command.argv),
            usage_json=json.dumps(exc.usage) if exc.usage else None,
        )
        store.update_task(conn, task["id"], status="blocked")
        raise DispatchError(f"stopped by supervisor: {exc.reason}", run_id=run_id) from exc
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
        raise DispatchError(str(exc), run_id=run_id) from exc

    _record_quota(conn, route.worker, result, run_id)
    # A write run is judged by the Phase 3 gate, which commits whatever the
    # agent left loose, checks it, and merges it into the integration branch if
    # every check passes. A run the worker itself failed is not gated: there is
    # nothing to judge, and the failure is the answer.
    gate: verification.GateReport | None = None
    if write and result.exit_code == 0:
        gate = verification.verify(
            conn, task, project=project, workspace=workspace, before=before,
            producer=route.worker, run_id=run_id,
            unattended=started_by == SCHEDULER,
            may_merge=verification.merge_allowed(project),
        )
    changed = gitutil.changed_files(workspace, before) if write else []
    size = gitutil.diff_size(workspace, before) if write else 0
    violations = (
        _gate_violations(gate) if gate is not None
        else _path_violations(changed, task["allowed_paths"]) if write else []
    )
    tests_passed: int | None = _gate_tests(gate)
    if write and gate is None and project["test_command"]:
        tests_passed = runs.execute_test_command(workspace, project["test_command"])

    failed = (
        result.exit_code != 0
        or bool(violations)
        or tests_passed == 0
        or (gate is not None and gate.status in {"rejected", "needs_owner"})
    )
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
        + (f"[cortex] path-scope violations: {', '.join(violations)}\n" if violations else "")
        + (f"[cortex] gate: {gate.status} - {gate.summary()}\n" if gate else ""),
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
        verification=gate.as_dict() if gate else None,
    )


def _gate_violations(gate: verification.GateReport) -> list[str]:
    check = next((item for item in gate.checks if item.name == "path_scope"), None)
    return list(check.evidence.get("violations", [])) if check else []


def _gate_tests(gate: verification.GateReport | None) -> int | None:
    """The gate's test result, preferring what the merged code did."""
    if gate is None:
        return None
    for name in ("merged_tests", "task_tests"):
        check = next((item for item in gate.checks if item.name == name), None)
        if check and check.status in {verification.PASS, verification.FAIL}:
            return 1 if check.status == verification.PASS else 0
    return None


def _with_owner_defaults(
    conn: sqlite3.Connection,
    route: routing.Route,
    task: sqlite3.Row,
    model_override: str | None,
    action_override: str | None,
) -> routing.Route:
    """Apply the owner's default model and level for this worker."""
    requested_model = task["requested_model"] if "requested_model" in task.keys() else None
    requested_effort = task["effort"] if "effort" in task.keys() else None
    model, effort = model_catalog.apply(
        conn, route.worker, model=route.model, effort=route.effort,
        model_requested=bool(model_override or requested_model),
        effort_requested=bool(requested_effort),
    )
    if model == route.model and effort == route.effort:
        return route
    return replace(route, model=model, effort=effort)


def _observe(run_id: str, text: str, observer: Callable[[str], None] | None) -> None:
    runlog.append(run_id, text)
    if observer is not None:
        observer(text)


def unattended_refusal(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    route: routing.Route,
    task: sqlite3.Row | None = None,
) -> str | None:
    """Why the scheduler may not start this route now, or None."""
    reason = autonomy.unattended_refusal(
        conn, project, write=route.action == "implement"
    )
    if reason:
        return reason
    if task is not None:
        reason = trading_guard.refusal(task)
        if reason:
            return reason
    reason = supervisor.worker_hold(conn, route.worker)
    if reason:
        return reason
    if route.worker in capacity.LOCAL:
        allowed, why = lanes.admit(conn, route.model)
        return None if allowed else why
    admission = capacity.admit(conn, route.worker)
    return None if admission.allowed else admission.reason


def admission_evidence(conn: sqlite3.Connection, route: routing.Route) -> dict[str, object]:
    """What the scheduler knew when it allowed a start, for the audit trail."""
    if route.worker in capacity.LOCAL:
        return {
            "lane": lanes.lane_for(route.model),
            "ninjatrader_running": lanes.ninjatrader_running(),
        }
    admission = capacity.admit(conn, route.worker)
    return {
        "reason": admission.reason,
        "reserve_pct": capacity.reserve_pct(conn),
        "windows": list(admission.windows),
    }


def _record_quota(
    conn: sqlite3.Connection, worker: str, result: workers.WorkerResult, run_id: str
) -> None:
    """Keep provider quota readings from every run; never fail the run for it."""
    try:
        capacity.record(conn, capacity.readings_from_output(
            worker, result.stdout, result.stderr,
            exit_code=result.exit_code, source_ref=run_id,
        ))
    except Exception as exc:  # accounting must not lose a finished run
        runlog.append(run_id, f"\n[cortex] quota reading not recorded: {exc}\n")


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
