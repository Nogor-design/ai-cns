"""Cortex-Lite CLI (spec section 9).

    cortex init <repo_path>            register a project, scaffold .cortex/state.md
    cortex brief <project>             compile a model-ready brief
    cortex run <task> --mode --model   start a run
    cortex done <run> | capture <run>  close a run, capture outcome
    cortex history <project>           model decision-support view
    cortex state <project> --regen     regenerate state.md from runs + decisions
    cortex decide <project> "..."      log a decision with rationale

Plus convenience commands: project list, task add/list, resolve.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import typer

from . import (
    brief as brief_mod,
    config,
    db,
    dispatcher,
    health,
    ids,
    routing as routing_mod,
    runs as runs_mod,
    state as state_mod,
    store,
    workers,
    webapp,
)
from .api_client import APIUnavailable

app = typer.Typer(
    help="Cortex-Lite: local memory + context compiler across projects and models.",
    no_args_is_help=True,
    add_completion=False,
)
task_app = typer.Typer(help="Manage tasks.", no_args_is_help=True)
project_app = typer.Typer(help="Manage projects.", no_args_is_help=True)
app.add_typer(task_app, name="task")
app.add_typer(project_app, name="project")


def _configure_console_encoding() -> None:
    """Make worker responses printable on Windows even when they contain Unicode."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


_configure_console_encoding()


def _conn() -> sqlite3.Connection:
    return db.connect()


def _err(msg: str) -> None:
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


# --------------------------------------------------------------------- init ---
@app.command()
def init(
    repo_path: str = typer.Argument(..., help="Absolute path to the repository."),
    name: str = typer.Option(None, "--name", help="Project name (default: dir name)."),
    stack: str = typer.Option(None, "--stack", help="Languages/frameworks/services."),
    goal: str = typer.Option(None, "--goal", help="Current objective."),
    test_command: str = typer.Option(
        None, "--test", help='Test command, e.g. "pytest -q".'
    ),
    program: str = typer.Option("general", "--program", help="Portfolio program/lane."),
    priority: int = typer.Option(3, "--priority", min=1, max=5, help="1 highest; 5 lowest."),
    privacy: str = typer.Option(
        "internal", "--privacy", help="public | internal | restricted."
    ),
    scaffold_state: bool = typer.Option(
        True, "--state/--no-state", help="Create .cortex/state.md in the repository."
    ),
):
    """Register a project and scaffold .cortex/state.md."""
    repo = Path(repo_path).expanduser().resolve()
    if not repo.exists():
        _err(f"repo path does not exist: {repo}")
    pname = name or repo.name
    conn = _conn()
    pid = ids.slugify(pname)
    if conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
        _err(f"project '{pid}' already exists.")
    store.create_project(
        conn,
        name=pname,
        repo_path=str(repo),
        stack=stack,
        current_goal=goal,
        test_command=test_command,
        program=program,
        priority=priority,
        privacy=privacy,
        state_mode="tracked" if scaffold_state else "deferred",
        project_id=pid,
    )
    project = store.get_project(conn, pid)
    typer.secho(f"registered project '{pid}'", fg=typer.colors.GREEN)
    typer.echo(f"  repo: {repo}")
    if scaffold_state:
        path = state_mod.write_state(repo, state_mod.scaffold(project))
        typer.echo(f"  state: {path}")
    else:
        typer.echo("  state: deferred (--no-state)")


# -------------------------------------------------------------------- brief ---
@app.command()
def brief(
    project: str = typer.Argument(..., help="Project id or name."),
    task: str = typer.Option(None, "--task", help="Task id to include."),
    model: str = typer.Option(None, "--model", help="Target model name."),
    mode: str = typer.Option(
        "manual", "--mode", help="agentic_cli | api | manual (tailors format)."
    ),
    no_ollama: bool = typer.Option(False, "--no-ollama", help="Skip Ollama PII pass."),
):
    """Compile a model-ready brief and print it (spec section 6)."""
    conn = _conn()
    try:
        proj = store.get_project(conn, project)
        task_row = store.get_task(conn, task) if task else None
    except store.NotFound as e:
        _err(str(e))
    try:
        compiled = brief_mod.compile_brief(
            conn, proj, task=task_row, model=model, mode=mode,
            use_ollama=not no_ollama,
        )
    except brief_mod.SecretsDetected as e:
        typer.secho(
            "brief BLOCKED by secret/privacy scan:", fg=typer.colors.RED, err=True
        )
        for f in e.findings:
            typer.echo(f"  - {f.kind} (line {f.line}): {f.snippet}", err=True)
        raise typer.Exit(code=2)
    typer.echo(compiled.text)


# ---------------------------------------------------------------------- run ---
@app.command()
def run(
    task: str = typer.Argument(..., help="Task id."),
    mode: str = typer.Option(..., "--mode", help="agentic_cli | api | manual."),
    model: str = typer.Option(None, "--model", help="Model name."),
    no_ollama: bool = typer.Option(False, "--no-ollama", help="Skip Ollama PII pass."),
):
    """Start a run for a task (spec section 7)."""
    conn = _conn()
    try:
        task_row = store.get_task(conn, task)
    except store.NotFound as e:
        _err(str(e))
    try:
        started = runs_mod.start_run(
            conn, task_row, mode=mode, model=model, use_ollama=not no_ollama
        )
    except brief_mod.SecretsDetected as e:
        typer.secho("run BLOCKED by secret/privacy scan:", fg=typer.colors.RED, err=True)
        for f in e.findings:
            typer.echo(f"  - {f.kind} (line {f.line}): {f.snippet}", err=True)
        raise typer.Exit(code=2)
    except APIUnavailable as e:
        _err(str(e))
    except ValueError as e:
        _err(str(e))

    typer.secho(f"run {started.run_id} started ({started.mode})", fg=typer.colors.GREEN)
    if started.response is not None:
        typer.echo("\n--- response ---")
        typer.echo(started.response)
    if started.note:
        typer.echo(f"\n{started.note}")


# --------------------------------------------------------------------- done ---
@app.command()
def done(
    run_id: str = typer.Argument(..., help="Run id to close (agentic)."),
    no_tests: bool = typer.Option(False, "--no-tests", help="Skip the test command."),
):
    """Close an agentic run: capture git diff + test results."""
    conn = _conn()
    try:
        run_row = store.get_run(conn, run_id)
    except store.NotFound as e:
        _err(str(e))
    if run_row["execution_mode"] != "agentic_cli":
        _err("`done` is for agentic_cli runs; use `capture` for manual runs.")
    result = runs_mod.finish_agentic(conn, run_row, run_tests=not no_tests)
    typer.secho(f"run {run_id} closed", fg=typer.colors.GREEN)
    typer.echo(f"  files changed: {len(result.files_changed)}")
    for f in result.files_changed[:20]:
        typer.echo(f"    - {f}")
    typer.echo(f"  diff size: {result.diff_size} lines")
    tp = result.tests_passed
    label = "passed" if tp == 1 else "failed" if tp == 0 else "not run"
    typer.echo(f"  tests: {label}")


# ------------------------------------------------------------------ capture ---
@app.command()
def capture(
    run_id: str = typer.Argument(..., help="Run id (manual)."),
    response_file: str = typer.Option(
        None, "--file", help="Read response from this file instead of stdin/editor."
    ),
    note: str = typer.Option(None, "--note", help="Optional human note."),
):
    """Capture a pasted-back response for a manual run."""
    conn = _conn()
    try:
        run_row = store.get_run(conn, run_id)
    except store.NotFound as e:
        _err(str(e))
    if response_file:
        text = Path(response_file).read_text(encoding="utf-8")
    else:
        text = typer.edit("\n# Paste the model response above this line.\n") or ""
        text = text.split("\n# Paste the model response")[0].strip()
    if not text.strip():
        _err("no response captured.")
    runs_mod.capture_manual(conn, run_row, text, note=note)
    typer.secho(f"captured response for run {run_id}", fg=typer.colors.GREEN)


# ------------------------------------------------------------------ history ---
@app.command()
def history(
    project: str = typer.Argument(..., help="Project id or name."),
    type: str = typer.Option(None, "--type", help="Filter by task type."),
):
    """Model decision-support view (spec section 8) — read-only."""
    conn = _conn()
    try:
        proj = store.get_project(conn, project)
    except store.NotFound as e:
        _err(str(e))
    rows = store.model_task_history(conn, proj["id"], task_type=type)
    if not rows:
        typer.echo("(no runs recorded yet)")
        return
    header = f"{'task_type':<10} {'model':<18} {'att':>3} {'pass':>4} {'surv':>4} {'avg_diff':>8}  last_used"
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in rows:
        avg = f"{r['avg_diff_size']:.0f}" if r["avg_diff_size"] is not None else "-"
        typer.echo(
            f"{r['task_type']:<10} {(r['model'] or '?'):<18} "
            f"{r['attempts']:>3} {r['tests_passed'] or 0:>4} {r['survived'] or 0:>4} "
            f"{avg:>8}  {(r['last_used'] or '')[:10]}"
        )


# -------------------------------------------------------------------- state ---
@app.command()
def state(
    project: str = typer.Argument(..., help="Project id or name."),
    regen: bool = typer.Option(False, "--regen", help="Regenerate state.md."),
    no_ollama: bool = typer.Option(False, "--no-ollama", help="Use deterministic builder only."),
):
    """Show or regenerate the canonical state.md (spec section 5)."""
    conn = _conn()
    try:
        proj = store.get_project(conn, project)
    except store.NotFound as e:
        _err(str(e))
    if regen:
        content = state_mod.regen(conn, proj, use_ollama=not no_ollama)
        store.update_project(conn, proj["id"])  # bump updated_at
        typer.secho(f"regenerated {proj['repo_path']}/.cortex/state.md", fg=typer.colors.GREEN)
        typer.echo(content)
        return
    current = state_mod.read_state(proj["repo_path"])
    if current is None:
        _err("no state.md found; run `cortex state <project> --regen` or `cortex init`.")
    typer.echo(current)


# ------------------------------------------------------------------- decide ---
@app.command()
def decide(
    project: str = typer.Argument(..., help="Project id or name."),
    decision: str = typer.Argument(..., help="The decision made."),
    why: str = typer.Option(None, "--why", help="Rationale."),
    source: str = typer.Option("manual", "--source", help="Provenance."),
):
    """Log a decision with rationale (spec section 4)."""
    conn = _conn()
    try:
        proj = store.get_project(conn, project)
    except store.NotFound as e:
        _err(str(e))
    did = store.create_decision(
        conn, project_id=proj["id"], decision=decision, rationale=why, source=source
    )
    typer.secho(f"logged decision {did}", fg=typer.colors.GREEN)


# ------------------------------------------------------------------ resolve ---
@app.command()
def resolve(project: str = typer.Argument(..., help="Project id or name.")):
    """Resolve survived/reverted outcomes for closed agentic runs."""
    conn = _conn()
    try:
        proj = store.get_project(conn, project)
    except store.NotFound as e:
        _err(str(e))
    results = runs_mod.resolve_outcomes(conn, proj)
    if not results:
        typer.echo("(nothing to resolve)")
        return
    for rid, outcome in results.items():
        typer.echo(f"  {rid}: {outcome}")


# ---------------------------------------------------------- portfolio / route ---
@app.command()
def dashboard(
    all_projects: bool = typer.Option(
        False, "--all", help="Include paused and archived projects."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    stale_days: int = typer.Option(14, "--stale-days", min=1),
):
    """Show portfolio health, Git state, and task pressure."""
    conn = _conn()
    rows = health.inspect_portfolio(
        conn,
        include_paused=all_projects,
        include_archived=all_projects,
        stale_days=stale_days,
    )
    if json_output:
        typer.echo(json.dumps([row.to_dict() for row in rows], indent=2))
        return
    if not rows:
        typer.echo("(no registered projects)")
        return
    header = (
        f"{'project':<22} {'program':<14} {'P':>1} {'health':<9} "
        f"{'git':<20} {'tasks O/R/B':<11} state"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for row in rows:
        git_label = "no-git"
        if row.is_git:
            git_label = f"{row.branch or '?'} d={row.modified + row.untracked}"
            if row.ahead is not None:
                git_label += f" +{row.ahead}/-{row.behind}"
        state_label = "missing" if not row.state_exists else f"{row.state_age_days}d"
        task_label = f"{row.open_tasks}/{row.review_tasks}/{row.blocked_tasks}"
        typer.echo(
            f"{row.project_id:<22.22} {row.program:<14.14} {row.priority:>1} "
            f"{row.health:<9} {git_label:<20.20} {task_label:<11} {state_label}"
        )


@app.command()
def digest(
    all_projects: bool = typer.Option(False, "--all", help="Include paused projects."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    output: Path = typer.Option(None, "--output", help="Also write the digest to this file."),
):
    """Produce the short daily decision digest."""
    conn = _conn()
    rows = health.inspect_portfolio(
        conn, include_paused=all_projects, include_archived=all_projects
    )
    if json_output:
        payload = {
            "ready_for_review": [r.to_dict() for r in rows if r.review_tasks],
            "needs_attention": [r.to_dict() for r in rows if r.warnings],
            "running": [r.to_dict() for r in rows if r.running_tasks],
            "clean": [r.to_dict() for r in rows if r.health == "clean"],
        }
        rendered = json.dumps(payload, indent=2)
        typer.echo(rendered)
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered + "\n", encoding="utf-8")
        return
    rendered = health.render_digest(rows)
    typer.echo(rendered)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        typer.echo(f"wrote: {output}")


@app.command()
def doctor():
    """Show the local control-plane paths and available worker CLIs."""
    typer.echo(f"database:  {config.db_path()}")
    typer.echo(f"worktrees: {config.work_root()}")
    typer.echo(f"run logs:  {config.run_root()}")
    typer.echo("workers:")
    for name in ("codex", "claude", "gemini", "grok", "ollama"):
        typer.echo(f"  {name:<8} {'ready' if workers.available(name) else 'missing'}")
    typer.echo("  perplexity manual/API (no local CLI configured)")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Local bind address."),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the dashboard in the default browser."
    ),
):
    """Start the local Cortex Portfolio dashboard."""
    webapp.serve(host=host, port=port, open_browser=open_browser)


@app.command("route")
def route_command(task_id: str = typer.Argument(..., help="Task id.")):
    """Explain the recommended worker, budget, and review gate for a task."""
    conn = _conn()
    try:
        task = store.get_task(conn, task_id)
        project = store.get_project(conn, task["project_id"])
    except store.NotFound as exc:
        _err(str(exc))
    route = routing_mod.route_task(project, task)
    typer.echo(f"task:       {task['title']}")
    typer.echo(f"risk:       {route.risk}")
    typer.echo(f"complexity: {route.complexity}/10")
    typer.echo(f"worker:     {route.worker}:{route.model}")
    typer.echo(f"action:     {route.action}")
    typer.echo(f"budget:     {route.budget}")
    typer.echo(f"reviewer:   {route.reviewer or '-'}")
    typer.echo(f"approval:   {'required' if route.requires_approval else 'normal gate'}")
    typer.echo("reasons:")
    for reason in route.reasons:
        typer.echo(f"  - {reason}")


@app.command()
def dispatch(
    task_id: str = typer.Argument(..., help="Task id."),
    execute: bool = typer.Option(False, "--execute", help="Actually start the worker."),
    allow_write: bool = typer.Option(
        False, "--allow-write", help="Allow an implementation run in an isolated worktree."
    ),
    approve_high_risk: bool = typer.Option(
        False, "--approve-high-risk", help="Explicit approval for restricted/high-risk routing."
    ),
    worker: str = typer.Option(None, "--worker", help="Override worker."),
    model: str = typer.Option(None, "--model", help="Override model."),
    action: str = typer.Option(None, "--action", help="review | research | draft | implement."),
    timeout: int = typer.Option(1800, "--timeout", min=30, help="Worker timeout in seconds."),
):
    """Preview by default; execute a bounded headless worker only with --execute."""
    conn = _conn()
    try:
        task = store.get_task(conn, task_id)
    except store.NotFound as exc:
        _err(str(exc))
    try:
        planned = dispatcher.preview(
            conn,
            task,
            worker_override=worker,
            model_override=model,
            action_override=action,
        )
    except (dispatcher.DispatchError, brief_mod.SecretsDetected) as exc:
        _err(str(exc))
    if not execute:
        typer.secho("DRY RUN — no worker started", fg=typer.colors.YELLOW)
        typer.echo(f"route:     {planned.route.worker}:{planned.route.model} ({planned.route.action})")
        typer.echo(f"risk:      {planned.route.risk}; complexity {planned.route.complexity}/10")
        typer.echo(f"workspace: {planned.workspace}")
        typer.echo(f"command:   {planned.command.display if planned.command else planned.note}")
        if planned.route.action == "implement":
            typer.echo("write safety: execution will require --allow-write and an isolated worktree")
        return
    try:
        result = dispatcher.dispatch(
            conn,
            task,
            worker_override=worker,
            model_override=model,
            action_override=action,
            allow_write=allow_write,
            approve_high_risk=approve_high_risk,
            timeout=timeout,
        )
    except (dispatcher.DispatchError, brief_mod.SecretsDetected) as exc:
        _err(str(exc))
    typer.secho(
        f"run {result.run_id} finished: {result.task_status}",
        fg=typer.colors.GREEN if result.task_status in {"done", "review"} else typer.colors.RED,
    )
    typer.echo(f"  worker: {result.worker}:{result.model}")
    typer.echo(f"  workspace: {result.workspace}")
    typer.echo(f"  exit code: {result.exit_code}")
    if result.files_changed:
        typer.echo(f"  files changed: {len(result.files_changed)}")
    if result.tests_passed is not None:
        typer.echo(f"  tests: {'passed' if result.tests_passed else 'failed'}")
    for path in result.violations:
        typer.echo(f"  path violation: {path}")


@app.command()
def result(
    run_id: str = typer.Argument(..., help="Run id."),
    json_output: bool = typer.Option(False, "--json", help="Emit the complete run record."),
):
    """Show a captured worker result and its verification evidence."""
    conn = _conn()
    try:
        run_row = store.get_run(conn, run_id)
        task = store.get_task(conn, run_row["task_id"])
        project = store.get_project(conn, run_row["project_id"])
    except store.NotFound as exc:
        _err(str(exc))
    if json_output:
        payload = {key: run_row[key] for key in run_row.keys()}
        payload["task_title"] = task["title"]
        payload["project_name"] = project["name"]
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"run:       {run_row['id']}")
    typer.echo(f"project:   {project['name']}")
    typer.echo(f"task:      {task['title']}")
    typer.echo(f"worker:    {run_row['model'] or '-'}")
    typer.echo(f"status:    {task['status']}")
    typer.echo(f"exit code: {run_row['exit_code'] if run_row['exit_code'] is not None else '-'}")
    typer.echo(f"workspace: {run_row['workspace_path'] or project['repo_path']}")
    if run_row["tests_passed"] is not None:
        typer.echo(f"tests:     {'passed' if run_row['tests_passed'] else 'failed'}")
    if run_row["files_changed"]:
        typer.echo(f"files:     {run_row['files_changed']}")
    typer.echo("\n--- worker response ---")
    typer.echo(run_row["response"] or "(no response captured)")
    if run_row["human_note"]:
        typer.echo("\n--- worker stderr / note ---")
        typer.echo(run_row["human_note"])


@app.command()
def judge(
    run_id: str = typer.Argument(..., help="Run id."),
    outcome: str = typer.Option(..., "--outcome", help="accepted | rejected."),
    note: str = typer.Option(None, "--note", help="Short acceptance/rework note."),
):
    """Accept or reject a captured worker result and update its task."""
    if outcome not in {"accepted", "rejected"}:
        _err("outcome must be accepted or rejected")
    conn = _conn()
    try:
        run_row = store.get_run(conn, run_id)
        task = store.get_task(conn, run_row["task_id"])
    except store.NotFound as exc:
        _err(str(exc))
    existing_note = run_row["human_note"] or ""
    combined_note = "\n".join(part for part in (existing_note, note) if part) or None
    store.update_run(conn, run_id, outcome=outcome, human_note=combined_note)
    store.update_task(conn, task["id"], status="done" if outcome == "accepted" else "open")
    typer.secho(f"run {run_id}: {outcome}", fg=typer.colors.GREEN)


# --------------------------------------------------------------- task / proj ---
@task_app.command("add")
def task_add(
    project: str = typer.Argument(..., help="Project id or name."),
    title: str = typer.Argument(..., help="Task title."),
    type: str = typer.Option("other", "--type", help="code|review|research|docs|data|planning|other."),
    brief: str = typer.Option(None, "--brief", help="Bounded task instructions."),
    risk: str = typer.Option("auto", "--risk", help="auto | low | medium | high."),
    complexity: int = typer.Option(None, "--complexity", min=0, max=10),
    acceptance: str = typer.Option(None, "--acceptance", help="Concrete acceptance check."),
    allowed_paths: str = typer.Option(
        None, "--allowed-paths", help="Comma/newline list or JSON array of allowed path globs."
    ),
    budget: str = typer.Option(None, "--budget", help="local | small | medium | large."),
    priority: int = typer.Option(3, "--priority", min=1, max=5),
    assignee: str = typer.Option(None, "--assignee", help="Explicit worker or owner."),
    due_at: str = typer.Option(None, "--due", help="Optional ISO date or datetime."),
):
    """Create a task and print its id."""
    conn = _conn()
    try:
        proj = store.get_project(conn, project)
    except store.NotFound as e:
        _err(str(e))
    tid = store.create_task(
        conn,
        project_id=proj["id"],
        title=title,
        type=type,
        brief=brief,
        risk=risk,
        complexity=complexity,
        acceptance=acceptance,
        allowed_paths=allowed_paths,
        budget=budget,
        priority=priority,
        assignee=assignee,
        due_at=due_at,
    )
    typer.secho(f"created task {tid}", fg=typer.colors.GREEN)
    typer.echo(tid)


@task_app.command("list")
def task_list(
    project: str = typer.Argument(..., help="Project id or name."),
    status: str = typer.Option(None, "--status", help="Filter by status."),
):
    """List tasks for a project."""
    conn = _conn()
    try:
        proj = store.get_project(conn, project)
    except store.NotFound as e:
        _err(str(e))
    rows = store.list_tasks(conn, proj["id"], status=status)
    if not rows:
        typer.echo("(no tasks)")
        return
    for t in rows:
        typer.echo(f"{t['id']}  [{t['status']:<11}] ({t['type']:<8}) {t['title']}")


@task_app.command("show")
def task_show(task_id: str = typer.Argument(..., help="Task id.")):
    """Show task controls and its current route."""
    conn = _conn()
    try:
        task = store.get_task(conn, task_id)
        project = store.get_project(conn, task["project_id"])
    except store.NotFound as exc:
        _err(str(exc))
    typer.echo(f"id:          {task['id']}")
    typer.echo(f"project:     {project['id']}")
    typer.echo(f"status:      {task['status']}")
    typer.echo(f"type:        {task['type']}")
    typer.echo(f"risk:        {task['risk']}")
    typer.echo(f"complexity:  {task['complexity'] if task['complexity'] is not None else 'auto'}")
    typer.echo(f"budget:      {task['budget'] or 'auto'}")
    typer.echo(f"title:       {task['title']}")
    typer.echo(f"acceptance:  {task['acceptance'] or '-'}")
    typer.echo(f"paths:       {task['allowed_paths'] or '-'}")
    typer.echo(f"instructions:{' ' + task['brief'] if task['brief'] else ' -'}")


@project_app.command("list")
def project_list():
    """List registered projects."""
    conn = _conn()
    rows = store.list_projects(conn)
    if not rows:
        typer.echo("(no projects; run `cortex init <repo_path>`)")
        return
    for p in rows:
        typer.echo(
            f"{p['id']:<20} [{p['status']:<8}] P{p['priority']} "
            f"{p['program']:<14} {p['privacy']:<10} {p['repo_path']}"
        )


@project_app.command("update")
def project_update(
    project: str = typer.Argument(..., help="Project id or name."),
    status: str = typer.Option(None, "--status", help="active | paused | archived."),
    program: str = typer.Option(None, "--program"),
    priority: int = typer.Option(None, "--priority", min=1, max=5),
    privacy: str = typer.Option(None, "--privacy", help="public | internal | restricted."),
    goal: str = typer.Option(None, "--goal"),
    test_command: str = typer.Option(None, "--test"),
):
    """Update portfolio metadata for a registered project."""
    conn = _conn()
    try:
        proj = store.get_project(conn, project)
    except store.NotFound as exc:
        _err(str(exc))
    fields = {
        key: value
        for key, value in {
            "status": status,
            "program": program,
            "priority": priority,
            "privacy": privacy,
            "current_goal": goal,
            "test_command": test_command,
        }.items()
        if value is not None
    }
    try:
        store.update_project(conn, proj["id"], **fields)
    except ValueError as exc:
        _err(str(exc))
    typer.secho(f"updated project {proj['id']}", fg=typer.colors.GREEN)


@task_app.command("update")
def task_update(
    task_id: str = typer.Argument(..., help="Task id."),
    status: str = typer.Option(None, "--status"),
    risk: str = typer.Option(None, "--risk"),
    complexity: int = typer.Option(None, "--complexity", min=0, max=10),
    acceptance: str = typer.Option(None, "--acceptance"),
    allowed_paths: str = typer.Option(None, "--allowed-paths"),
    budget: str = typer.Option(None, "--budget"),
    priority: int = typer.Option(None, "--priority", min=1, max=5),
    assignee: str = typer.Option(None, "--assignee"),
    due_at: str = typer.Option(None, "--due"),
):
    """Update a task's routing controls or lifecycle state."""
    conn = _conn()
    try:
        task = store.get_task(conn, task_id)
    except store.NotFound as exc:
        _err(str(exc))
    fields = {
        key: value
        for key, value in {
            "status": status,
            "risk": risk,
            "complexity": complexity,
            "acceptance": acceptance,
            "allowed_paths": allowed_paths,
            "budget": budget,
            "priority": priority,
            "assignee": assignee,
            "due_at": due_at,
        }.items()
        if value is not None
    }
    try:
        store.update_task(conn, task["id"], **fields)
    except ValueError as exc:
        _err(str(exc))
    typer.secho(f"updated task {task['id']}", fg=typer.colors.GREEN)


if __name__ == "__main__":  # pragma: no cover
    app()
