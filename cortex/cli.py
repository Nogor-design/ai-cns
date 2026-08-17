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
    git_monitor,
    github_adapter,
    github_reader,
    health,
    ids,
    pm as pm_mod,
    policy,
    project_blueprints,
    project_registration,
    routing as routing_mod,
    runs as runs_mod,
    state as state_mod,
    store,
    team,
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
pm_session_app = typer.Typer(help="Track durable PM sessions and attribution.", no_args_is_help=True)
activity_app = typer.Typer(help="Inspect append-only portfolio activity.", no_args_is_help=True)
github_app = typer.Typer(
    help="Inventory, dry-run, and explicitly apply the GitHub Project mirror.",
    no_args_is_help=True,
)
app.add_typer(task_app, name="task")
app.add_typer(project_app, name="project")
app.add_typer(pm_session_app, name="pm")
app.add_typer(activity_app, name="activity")
app.add_typer(github_app, name="github")


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
    for name in ("codex", "claude", "gemini", "grok", "ollama", "perplexity"):
        probe = workers.probe(name)
        typer.echo(f"  {name:<11} {probe['availability']:<10} {probe.get('note') or ''}")


@app.command("team")
def team_command():
    """Show each expert's role, availability, queue, and measured performance."""
    conn = _conn()
    payload = webapp.portfolio_payload(conn)
    for member in payload["team"]:
        current = member["current_task"]
        typer.secho(
            f"{member['name']:<11} {member['state']:<12} {member['role']}",
            fg=typer.colors.CYAN if member["state"] == "working" else None,
        )
        if current:
            typer.echo(
                f"  next: {current['id']}  {current['title']} "
                f"[{current.get('execution_model') or 'default'}, "
                f"{current.get('execution_effort') or 'auto'} effort]"
            )
        else:
            typer.echo(f"  {member.get('probe_note') or 'ready for a suitable assignment'}")
        typer.echo(
            f"  evidence: {member['attempts']} attempts; "
            f"{member['success_rate'] if member['success_rate'] is not None else '-'}% success; "
            f"{member['tokens']} tracked tokens"
        )


@app.command("git-check")
def git_check(fetch: bool = typer.Option(True, "--fetch/--no-fetch")):
    """Refresh stored Git working-tree and remote synchronization evidence."""
    conn = _conn()
    rows = git_monitor.refresh_portfolio(conn, fetch=fetch)
    for row in rows:
        state = "not-git" if not row["is_git"] else (
            f"dirty={row['modified'] + row['untracked']} ahead={row['ahead']} behind={row['behind']}"
        )
        typer.echo(f"{row['project_id']:<24} {state}")


@app.command("keep-working")
def keep_working(
    execute: bool = typer.Option(False, "--execute", help="Start safe queued assignments."),
    limit: int = typer.Option(3, "--limit", min=1, max=6),
):
    """Preview or start one approved read-only assignment per available expert."""
    conn = _conn()
    task_ids = team.safe_start_candidates(conn, limit=limit)
    if not task_ids:
        typer.echo("No safe approved assignments are waiting. Run `cortex focus` or plan a project.")
        return
    for task_id in task_ids:
        task = store.get_task(conn, task_id)
        typer.echo(f"{task_id}  {task['assignee']}  {task['title']}")
    if not execute:
        typer.echo("preview only; add --execute to start these assignments")
        return
    for task_id in task_ids:
        result = dispatcher.dispatch(conn, store.get_task(conn, task_id))
        typer.echo(f"{result.run_id}  {result.worker}:{result.model}  {result.task_status}")


# ---------------------------------------------------------- AI PM workflow ---
@app.command()
def focus(
    project: str = typer.Argument(None, help="Optional project id or name."),
):
    """Show the one thing that should receive attention next (no AI tokens)."""
    conn = _conn()
    project_id = None
    if project:
        try:
            project_id = store.get_project(conn, project)["id"]
        except store.NotFound as exc:
            _err(str(exc))
    result = pm_mod.portfolio_focus(conn, project_id)
    item = result["focus"]
    kind = item["kind"]
    if kind == "empty":
        typer.echo("No active project needs attention.")
        return
    if kind in {"decision", "work", "task"}:
        task = store.get_task(conn, item["id"])
        project_row = store.get_project(conn, task["project_id"])
        heading = {
            "decision": "NEEDS YOUR DECISION",
            "work": "AI WORKING / READY TO START",
            "task": "NEXT READY TASK",
        }[kind]
        typer.secho(heading, fg=typer.colors.YELLOW if kind == "decision" else typer.colors.CYAN)
        typer.echo(f"project: {project_row['name']}")
        typer.echo(f"task:    {task['id']}  {task['title']}")
        typer.echo(f"status:  {task['status']}")
        if kind in {"work", "task"}:
            if str(task["assignee"] or "").lower() in {"owner", "perplexity"}:
                typer.echo("next:    complete this manually, then update its status")
            else:
                typer.echo(f"next:    cortex dispatch {task['id']} --execute")
        else:
            typer.echo("next:    review the captured result or unblock the task")
        return
    if kind == "suggestion":
        suggestion = store.get_suggestion(conn, item["id"])
        typer.secho("RECOMMENDED NEXT MOVE", fg=typer.colors.GREEN)
        typer.echo(f"project: {suggestion['project_name'] if 'project_name' in suggestion.keys() else item['project_id']}")
        typer.echo(f"idea:    {suggestion['id']}  {suggestion['title']}")
        typer.echo(f"why:     {suggestion['why']}")
        typer.echo(f"next:    cortex approve {suggestion['id']} --start")
        return
    project_row = store.get_project(conn, item["project_id"])
    typer.secho("PLAN NEEDED", fg=typer.colors.CYAN)
    typer.echo(f"project: {project_row['name']}")
    typer.echo(f"next:    cortex plan {project_row['id']}")


@app.command("next")
def next_action(
    project: str = typer.Argument(None, help="Optional project id or name."),
):
    """Alias for `cortex focus`: show the next useful action without planning."""
    focus(project)


@app.command("plan")
def plan_command(
    project: str = typer.Argument(None, help="Project id/name; defaults to top active project."),
    worker: str = typer.Option("codex", "--worker", help="codex or ollama."),
    model: str = typer.Option("default", "--model", help="Optional worker model."),
    count: int = typer.Option(3, "--count", min=1, max=3),
    force: bool = typer.Option(False, "--force", help="Replace current unapproved suggestions."),
    strict: bool = typer.Option(False, "--strict", help="Fail instead of using the no-token fallback."),
    allow_cloud: bool = typer.Option(
        False, "--allow-cloud", help="Allow Codex planning for a restricted project."
    ),
):
    """Ask an AI PM for bounded next moves; current suggestions are cached."""
    if worker not in {"codex", "ollama"}:
        _err("planning worker must be codex or ollama")
    conn = _conn()
    try:
        if project:
            project_row = store.get_project(conn, project)
        else:
            active = sorted(
                (row for row in store.list_projects(conn) if row["status"] == "active"),
                key=lambda row: (row["priority"], row["updated_at"]),
            )
            if not active:
                _err("no active projects")
            project_row = active[0]
        result = pm_mod.plan_project(
            conn, project_row, worker=worker, model=model, count=count,
            force=force, strict=strict, allow_cloud=allow_cloud,
        )
    except (store.NotFound, pm_mod.PlanningError) as exc:
        _err(str(exc))
    source = result.source_worker
    typer.secho(
        f"{'reused' if result.cached else 'created'} {len(result.suggestion_ids)} suggestions for {project_row['name']}",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"planner: {source}{':' + result.source_model if result.source_model else ''}")
    if result.note:
        typer.echo(f"note:    {result.note}")
    for sid in result.suggestion_ids:
        row = store.get_suggestion(conn, sid)
        typer.echo(f"\n{row['id']}  {row['title']}")
        typer.echo(f"  why:        {row['why']}")
        typer.echo(f"  acceptance: {row['acceptance']}")
        typer.echo(
            f"  route:      {row['recommended_worker']}:{row['recommended_model']} "
            f"({row['action']}, {row['budget']})"
        )
    typer.echo(f"\nApprove one: cortex approve {result.suggestion_ids[0]} --start")


@app.command()
def approve(
    suggestion_id: str = typer.Argument(..., help="Suggestion id."),
    start: bool = typer.Option(False, "--start/--queue", help="Start now or only queue it."),
    allow_write: bool = typer.Option(
        False, "--allow-write", help="Allow implementation in an isolated worktree."
    ),
    approve_high_risk: bool = typer.Option(
        False, "--approve-high-risk", help="Explicitly approve a high-risk/restricted route."
    ),
):
    """Approve a recommendation, assign it, and optionally start its worker."""
    conn = _conn()
    try:
        task_id = store.convert_suggestion(conn, suggestion_id)
        task = store.get_task(conn, task_id)
    except (store.NotFound, ValueError) as exc:
        _err(str(exc))
    typer.secho(f"approved and assigned task {task_id}", fg=typer.colors.GREEN)
    if not start:
        typer.echo(f"start later: cortex dispatch {task_id} --execute")
        return
    try:
        result = dispatcher.dispatch(
            conn, task, allow_write=allow_write,
            approve_high_risk=approve_high_risk,
        )
    except (dispatcher.DispatchError, brief_mod.SecretsDetected) as exc:
        typer.secho(f"queued, but not started: {exc}", fg=typer.colors.YELLOW)
        typer.echo(f"task remains assigned: {task_id}")
        raise typer.Exit(code=2)
    typer.secho(
        f"run {result.run_id} finished: {result.task_status}",
        fg=typer.colors.GREEN if result.task_status in {"done", "review"} else typer.colors.RED,
    )


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
    typer.echo(f"effort:     {route.effort}")
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
        typer.echo(f"effort:    {planned.route.effort}")
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
    model: str = typer.Option(None, "--model", help="Requested model for this assignment."),
    effort: str = typer.Option(None, "--effort", help="low | medium | high | xhigh."),
    due_at: str = typer.Option(None, "--due", help="Optional ISO date or datetime."),
    parent_id: str = typer.Option(None, "--parent"),
    milestone: str = typer.Option(None, "--milestone"),
    next_action: str = typer.Option(None, "--next-action"),
    thread_id: str = typer.Option(None, "--thread"),
    github_issue_url: str = typer.Option(None, "--github-issue"),
    actor: str = typer.Option("owner", "--actor"),
    actor_type: str = typer.Option("human", "--actor-type", help="human | agent | system"),
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
        requested_model=model,
        effort=effort,
        due_at=due_at,
        parent_id=parent_id,
        milestone=milestone,
        next_action=next_action,
        codex_thread_id=thread_id,
        github_issue_url=github_issue_url,
        actor_type=actor_type,
        actor_name=actor,
        source="cli",
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


@task_app.command("depend")
def task_depend(
    task_id: str = typer.Argument(..., help="Blocked task id."),
    depends_on: str = typer.Argument(..., help="Task id that must finish first."),
    actor: str = typer.Option("codex", "--actor", help="Who recorded the dependency."),
):
    """Record a finish-to-start blocking dependency."""
    conn = _conn()
    try:
        store.add_task_dependency(
            conn, task_id, depends_on, actor_name=actor, source="cli"
        )
    except (store.NotFound, ValueError) as exc:
        _err(str(exc))
    typer.secho(f"{task_id} is blocked by {depends_on}", fg=typer.colors.GREEN)


@task_app.command("undepend")
def task_undepend(
    task_id: str = typer.Argument(..., help="Previously blocked task id."),
    depends_on: str = typer.Argument(..., help="Dependency task id to remove."),
    actor: str = typer.Option("codex", "--actor"),
):
    """Remove a blocking dependency and preserve the change in activity."""
    conn = _conn()
    try:
        store.remove_task_dependency(
            conn, task_id, depends_on, actor_name=actor, source="cli"
        )
    except (store.NotFound, ValueError) as exc:
        _err(str(exc))
    typer.secho(f"removed dependency {task_id} -> {depends_on}", fg=typer.colors.GREEN)


@activity_app.command("list")
def activity_list(
    project: str = typer.Option(None, "--project", help="Project id or name."),
    task_id: str = typer.Option(None, "--task"),
    session_id: str = typer.Option(None, "--session"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    as_json: bool = typer.Option(False, "--json"),
):
    """List activity by project, work item, or PM session."""
    conn = _conn()
    project_id = None
    if project:
        try:
            project_id = store.get_project(conn, project)["id"]
        except store.NotFound as exc:
            _err(str(exc))
    rows = store.list_activity_events(
        conn, project_id, task_id=task_id, session_id=session_id, limit=limit
    )
    payload = [{key: row[key] for key in row.keys()} for row in rows]
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    if not payload:
        typer.echo("(no activity)")
        return
    for event in payload:
        typer.echo(
            f"{event['occurred_at']}  {event['actor_name'] or event['actor_type']:<12} "
            f"{event['action']:<24} {event['summary']}"
        )


# ----------------------------------------------------------- GitHub mirror ---
def _github_target(project: sqlite3.Row) -> tuple[str, int]:
    owner = project["github_project_owner"]
    number = project["github_project_number"]
    if not owner or number is None:
        _err(
            "GitHub Project is not configured; run `cortex github configure "
            f"{project['id']} --owner <login> --number <number>`"
        )
    return str(owner), int(number)


@github_app.command("configure")
def github_configure(
    project: str = typer.Argument(..., help="Cortex project id or name."),
    owner: str = typer.Option(..., "--owner", help="GitHub user or organization login."),
    number: int = typer.Option(..., "--number", min=1, help="GitHub Project number."),
    actor: str = typer.Option("owner", "--actor"),
):
    """Verify and save the exact GitHub Project target for one Cortex project."""
    conn = _conn()
    try:
        project_row = store.get_project(conn, project)
        snapshot = github_reader.read_project(owner, number)
        if snapshot.get("project_closed"):
            raise ValueError("configured GitHub Project is closed")
        store.update_project(
            conn,
            project_row["id"],
            github_project_owner=owner,
            github_project_number=number,
            github_project_id=snapshot["project_id"],
        )
        store.create_activity_event(
            conn,
            project_id=project_row["id"],
            actor_type="human" if actor == "owner" else "agent",
            actor_name=actor,
            action="github.mirror_configured",
            summary=(
                f"Configured GitHub Project {owner} #{number} "
                f"({snapshot.get('project_title')})"
            ),
            source="github-mirror",
            source_ref=snapshot["project_id"],
            evidence={
                "owner": owner,
                "number": number,
                "project_id": snapshot["project_id"],
            },
        )
    except (store.NotFound, ValueError, github_reader.GitHubProjectError) as exc:
        _err(str(exc))
    typer.secho(
        f"configured {project_row['id']} -> {owner} Project #{number} "
        f"({snapshot['project_id']})",
        fg=typer.colors.GREEN,
    )


@github_app.command("inventory")
def github_inventory(
    project: str = typer.Argument(..., help="Cortex project id or name."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Read the complete configured Project without writing local or remote state."""
    conn = _conn()
    try:
        project_row = store.get_project(conn, project)
        owner, number = _github_target(project_row)
        snapshot = github_reader.read_project(owner, number)
        if project_row["github_project_id"] != snapshot["project_id"]:
            raise ValueError("configured GitHub Project node ID changed")
    except (store.NotFound, ValueError, github_reader.GitHubProjectError) as exc:
        _err(str(exc))
    if as_json:
        typer.echo(json.dumps(snapshot, indent=2, sort_keys=True))
        return
    typer.echo(
        f"{snapshot['project_title']} ({snapshot['project_id']}): "
        f"{len(snapshot['items'])} items, {len(snapshot['field_schema'])} fields"
    )
    typer.echo("items_complete: true; field_schema_complete: true")


@github_app.command("link")
def github_link(
    task_id: str = typer.Argument(..., help="Cortex task to link."),
    issue_url: str = typer.Argument(..., help="Exact existing GitHub issue URL."),
    actor: str = typer.Option("owner", "--actor"),
):
    """Resolve and store one existing issue's stable ID; never create an issue."""
    conn = _conn()
    try:
        task = store.get_task(conn, task_id)
        project = store.get_project(conn, task["project_id"])
        if task["github_issue_id"]:
            raise ValueError("task already has a stable GitHub issue link")
        if not project["github_owner"] or not project["github_repo"]:
            raise ValueError(
                "project repository identity is not configured; use `project update "
                f"{project['id']} --github-owner <owner> --github-repo <repo>`"
            )
        issue = github_reader.read_issue(issue_url)
        expected_repo = f"{project['github_owner']}/{project['github_repo']}"
        if str(issue.get("repository") or "").lower() != expected_repo.lower():
            raise ValueError(
                f"issue belongs to {issue.get('repository')}, expected {expected_repo}"
            )
        store.update_task(
            conn,
            task_id,
            github_issue_id=issue["id"],
            github_issue_number=issue["number"],
            github_issue_url=issue["url"],
            actor_type="human" if actor == "owner" else "agent",
            actor_name=actor,
            source="github-mirror",
            commit=False,
        )
        store.create_activity_event(
            conn,
            project_id=task["project_id"],
            task_id=task_id,
            actor_type="human" if actor == "owner" else "agent",
            actor_name=actor,
            action="github.issue_linked",
            summary=f"Linked existing GitHub issue #{issue['number']}",
            source="github-mirror",
            source_ref=issue["id"],
            session_id=task["pm_session_id"],
            evidence={
                "issue_id": issue["id"],
                "issue_number": issue["number"],
                "issue_url": issue["url"],
                "repository": issue["repository"],
            },
            commit=False,
        )
        conn.commit()
    except (store.NotFound, ValueError, github_reader.GitHubProjectError) as exc:
        conn.rollback()
        _err(str(exc))
    typer.secho(
        f"linked {task_id} -> {issue['repository']}#{issue['number']} ({issue['id']})",
        fg=typer.colors.GREEN,
    )


@github_app.command("plan")
def github_plan(
    task_id: str = typer.Argument(..., help="Exactly one linked Cortex task."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Print a strict dry-run plan without changing portfolio or GitHub records."""
    conn = _conn()
    try:
        task = store.get_task(conn, task_id)
        project = store.get_project(conn, task["project_id"])
        owner, number = _github_target(project)
        plan, snapshot = github_adapter.prepare(task, owner, number)
        if project["github_project_id"] != snapshot["project_id"]:
            raise ValueError("configured GitHub Project node ID changed")
    except (
        store.NotFound, ValueError, github_reader.GitHubProjectError,
        github_adapter.MirrorApplyError,
    ) as exc:
        _err(str(exc))
    payload = plan.as_dict()
    payload["operation_id"] = github_adapter.suggested_operation_id(plan.fingerprint)
    payload["target"] = {"owner": owner, "number": number}
    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"task:        {task_id}")
    typer.echo(f"target:      {owner} Project #{number} ({plan.project_id})")
    typer.echo(f"issue:       {plan.issue_id or '-'}")
    typer.echo(f"item:        {plan.project_item_id or '-'}")
    typer.echo(f"safe:        {'yes' if plan.safe_to_apply else 'no'}")
    typer.echo(f"fingerprint: {plan.fingerprint}")
    typer.echo(f"operation:   {payload['operation_id']}")
    for action in plan.actions:
        typer.echo(
            f"  {action.kind:<25} {action.field or action.target_id} "
            f"{json.dumps(action.value, sort_keys=True)}"
        )
    for conflict in plan.conflicts:
        typer.echo(f"  CONFLICT {conflict.code}: {conflict.summary}")
    if plan.safe_to_apply and plan.actions:
        typer.echo("\nApply only this exact re-read plan:")
        typer.echo(
            f"cortex github apply {task_id} --approve {plan.fingerprint} "
            f"--operation-id {payload['operation_id']}"
        )
    elif not plan.actions and plan.safe_to_apply:
        typer.secho("already converged; no apply is needed", fg=typer.colors.GREEN)


@github_app.command("apply")
def github_apply(
    task_id: str = typer.Argument(..., help="Exactly one linked Cortex task."),
    approved_fingerprint: str = typer.Option(
        ..., "--approve", help="Exact fingerprint printed by `github plan`."
    ),
    operation_id: str = typer.Option(
        ..., "--operation-id", help="Exact operation id printed by `github plan`."
    ),
    actor: str = typer.Option("codex", "--actor"),
):
    """Apply or resume one explicitly approved plan; never bulk or schedule work."""
    conn = _conn()
    try:
        task = store.get_task(conn, task_id)
        project = store.get_project(conn, task["project_id"])
        owner, number = _github_target(project)
        result = github_adapter.apply(
            conn,
            task_id,
            owner,
            number,
            approved_fingerprint=approved_fingerprint,
            operation_id=operation_id,
            actor=actor,
        )
    except (
        store.NotFound, ValueError, github_reader.GitHubProjectError,
        github_adapter.MirrorApplyError,
    ) as exc:
        _err(str(exc))
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@github_app.command("operation")
def github_operation(
    operation_id: str = typer.Argument(..., help="Durable mirror operation id."),
):
    """Inspect replay, recovery, and verification evidence for one operation."""
    conn = _conn()
    try:
        row = store.get_github_mirror_operation(conn, operation_id)
    except store.NotFound as exc:
        _err(str(exc))
    payload = {key: row[key] for key in row.keys()}
    for key in ("actions_json", "completed_actions_json", "evidence_json"):
        if payload.get(key):
            payload[key.removesuffix("_json")] = json.loads(payload.pop(key))
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@pm_session_app.command("start")
def pm_session_start(
    project: str = typer.Argument(..., help="Project id or name."),
    title: str = typer.Argument(..., help="Current bounded PM slice."),
    task_id: str = typer.Option(None, "--task", help="Continue an existing work item."),
    owner: str = typer.Option("codex", "--owner", help="Accountable PM or agent."),
    acceptance: str = typer.Option(None, "--acceptance", help="Observable done-when check."),
    next_action: str = typer.Option(None, "--next-action", help="Current explicit next action."),
    thread_id: str = typer.Option(None, "--thread", help="Linked Codex task/thread id."),
):
    """Start or continue a PM session and persist its attribution."""
    conn = _conn()
    try:
        project_row = store.get_project(conn, project)
        tracked_task, session_id = store.start_pm_session(
            conn,
            project_id=project_row["id"],
            title=title,
            task_id=task_id,
            owner=owner,
            acceptance=acceptance,
            next_action=next_action,
            codex_thread_id=thread_id,
            source="cli-pm",
        )
    except (store.NotFound, ValueError) as exc:
        _err(str(exc))
    typer.secho(f"PM session {session_id} started", fg=typer.colors.GREEN)
    typer.echo(f"task {tracked_task}")


@pm_session_app.command("event")
def pm_session_event(
    task_id: str = typer.Argument(..., help="Tracked PM work item."),
    action: str = typer.Argument(..., help="Stable event action, e.g. delegation.started."),
    summary: str = typer.Argument(..., help="Human-readable attribution summary."),
    actor: str = typer.Option("codex", "--actor"),
    actor_type: str = typer.Option("agent", "--actor-type"),
    model: str = typer.Option(None, "--model"),
    source_ref: str = typer.Option(None, "--ref", help="Run, commit, PR, or artifact id."),
):
    """Append an attributed event to the current PM session."""
    conn = _conn()
    try:
        task = store.get_task(conn, task_id)
        event_id = store.create_activity_event(
            conn,
            project_id=task["project_id"],
            task_id=task_id,
            actor_type=actor_type,
            actor_name=actor,
            model=model,
            action=action,
            summary=summary,
            source="cli-pm",
            source_ref=source_ref,
            session_id=task["pm_session_id"],
        )
    except store.NotFound as exc:
        _err(str(exc))
    typer.secho(f"recorded event {event_id}", fg=typer.colors.GREEN)


@pm_session_app.command("close")
def pm_session_close(
    task_id: str = typer.Argument(..., help="Tracked PM work item."),
    summary: str = typer.Argument(..., help="Accepted result, blocker, or continuation state."),
    status: str = typer.Option("done", "--status", help="done | review | blocked | in_progress."),
    owner: str = typer.Option("codex", "--owner"),
    next_action: str = typer.Option(None, "--next-action", help="Exactly one recommended continuation."),
):
    """Close a PM session with its outcome and next action."""
    conn = _conn()
    try:
        session_id = store.close_pm_session(
            conn,
            task_id,
            status=status,
            summary=summary,
            owner=owner,
            next_action=next_action,
            source="cli-pm",
        )
    except (store.NotFound, ValueError) as exc:
        _err(str(exc))
    typer.secho(f"PM session {session_id} closed as {status}", fg=typer.colors.GREEN)


@project_app.command("list")
def project_list():
    """List registered projects."""
    conn = _conn()
    rows = store.list_projects(conn)
    if not rows:
        typer.echo("(no projects; run `cortex init <repo_path>`)")
        return
    for p in rows:
        allowed = ",".join(policy.allowed_workers(p))
        marker = " " if policy.is_configured(p) else "*"
        typer.echo(
            f"{p['id']:<20} [{p['status']:<8}] P{p['priority']} "
            f"{p['program']:<14} {p['privacy']:<10} {marker}{allowed:<28} {p['repo_path']}"
        )
    typer.echo("\n* = default allowlist; set one with `cortex project workers <id> ...`")


@project_app.command("onboard")
def project_onboard(
    repo_path: str = typer.Argument(..., help="Absolute project folder to onboard."),
    name: str = typer.Option(None, "--name", help="Project name for a new registration."),
    privacy: str = typer.Option(None, "--privacy", help="public | internal | restricted."),
    workers_csv: str = typer.Option(None, "--workers", help="Comma-separated worker allowlist."),
    scaffold_state: bool = typer.Option(
        True, "--state/--no-state", help="Create a starter state file for a new project."
    ),
    draft_only: bool = typer.Option(
        False, "--draft-only", help="Save answers and preview without asking for approval."
    ),
):
    """Guide one project from local discovery to an approved blueprint."""
    conn = _conn()
    try:
        registration_preview = project_registration.preview(conn, repo_path)
        if registration_preview["already_registered"]:
            project_id = registration_preview["existing_project_id"]
        else:
            project_name = name or typer.prompt(
                "Project name", default=registration_preview["suggested_name"]
            )
            selected_privacy = privacy or typer.prompt("Privacy", default="internal")
            selected_privacy = selected_privacy.strip().lower()
            if selected_privacy not in store.PRIVACY_LEVELS:
                raise ValueError("privacy must be public, internal, or restricted")
            default_workers = policy.DEFAULT_BY_PRIVACY[selected_privacy]
            selected_workers = workers_csv or typer.prompt(
                "Workers allowed to read this repository",
                default=",".join(default_workers),
            )
            workers_list = [
                part.strip()
                for part in selected_workers.replace(",", " ").split()
                if part.strip()
            ]
            registered = project_registration.register(conn, {
                "repo_path": registration_preview["repo_path"],
                "name": project_name,
                "program": "general",
                "priority": 3,
                "privacy": selected_privacy,
                "stack": registration_preview["stack"],
                "test_command": registration_preview["test_command"],
                "allowed_workers": workers_list,
                "track_state": scaffold_state,
            })
            project_id = registered["project"]["id"]
            typer.secho(f"registered project '{project_id}'", fg=typer.colors.GREEN)

        draft = project_blueprints.draft_detail(conn, project_id)
        prepared = draft.get("preview") if draft["stage"] == "review" else None
        if prepared:
            typer.echo("\nResuming the exact saved blueprint approval preview.")
        else:
            answers = dict(draft["answers"])
            typer.echo(
                "\nCortex inspected bounded local repository evidence and contacted no provider."
            )
            typer.echo(f"discovery: {draft['discovery_hash']}")
            for question in draft["questions"]:
                current = answers.get(question["key"])
                answers[question["key"]] = typer.prompt(
                    question["label"], default=current or None
                ).strip()
            typer.echo("\nCurrent phase")
            for question in draft["phase_questions"]:
                current = answers.get(question["key"])
                answers[question["key"]] = typer.prompt(
                    question["label"], default=current or None
                ).strip()
            project_blueprints.begin_draft(
                conn, project_id, answers=answers, stage="planning"
            )
            project = store.get_project(conn, project_id)
            if not project["current_goal"]:
                store.update_project(
                    conn, project_id, current_goal=answers["desired_outcome"]
                )
            prepared = project_blueprints.draft_preview(conn, project_id)
    except (store.NotFound, ValueError) as exc:
        _err(str(exc))

    typer.echo("\n--- Blueprint approval preview ---\n")
    typer.echo(prepared["markdown"])
    typer.echo("Phases:")
    for phase in prepared["phases"]:
        typer.echo(f"  {phase['ordinal']}. [{phase['status']}] {phase['name']} — {phase['outcome']}")
    typer.echo(f"Preview fingerprint: {prepared['preview_fingerprint']}")
    typer.echo("No execution tasks were generated and no provider was contacted.")
    if draft_only or not typer.confirm("Approve this exact blueprint and activate its first phase?"):
        typer.secho("draft saved in review; repository blueprint was not written", fg=typer.colors.YELLOW)
        return
    try:
        approved = project_blueprints.approve_draft(
            conn,
            project_id,
            preview_fingerprint=prepared["preview_fingerprint"],
            approving_actor="owner",
        )
    except ValueError as exc:
        _err(str(exc))
    typer.secho(
        f"approved blueprint revision {approved['revision']['ordinal']} for {project_id}",
        fg=typer.colors.GREEN,
    )


@project_app.command("workers")
def project_workers(
    project: str = typer.Argument(..., help="Project id or name."),
    workers_csv: str = typer.Argument(
        None,
        metavar="[WORKERS]",
        help="Comma-separated worker list, e.g. claude,ollama. Omit to show.",
    ),
):
    """Show or set which AI workers may read this project's repository."""
    conn = _conn()
    try:
        proj = store.get_project(conn, project)
    except store.NotFound as exc:
        _err(str(exc))
    if not workers_csv:
        typer.echo(f"{proj['id']}: {', '.join(policy.allowed_workers(proj))}")
        if not policy.is_configured(proj):
            typer.echo(f"(default for privacy={proj['privacy']}; not explicitly set)")
        return
    names = [part.strip() for part in workers_csv.replace(",", " ").split() if part.strip()]
    try:
        store.update_project(conn, proj["id"], allowed_workers=policy.encode(names))
    except ValueError as exc:
        _err(str(exc))
    updated = store.get_project(conn, proj["id"])
    typer.secho(
        f"{proj['id']} may now use: {', '.join(policy.allowed_workers(updated))}",
        fg=typer.colors.GREEN,
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
    remote_url: str = typer.Option(None, "--remote-url"),
    github_owner: str = typer.Option(None, "--github-owner"),
    github_repo: str = typer.Option(None, "--github-repo"),
    codex_project_id: str = typer.Option(None, "--codex-project"),
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
            "remote_url": remote_url,
            "github_owner": github_owner,
            "github_repo": github_repo,
            "codex_project_id": codex_project_id,
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
    parent_id: str = typer.Option(None, "--parent"),
    milestone: str = typer.Option(None, "--milestone"),
    progress: int = typer.Option(None, "--progress", min=0, max=100),
    blocked_reason: str = typer.Option(None, "--blocked-reason"),
    next_action: str = typer.Option(None, "--next-action"),
    thread_id: str = typer.Option(None, "--thread"),
    github_issue_url: str = typer.Option(None, "--github-issue"),
    actor: str = typer.Option("owner", "--actor"),
    actor_type: str = typer.Option("human", "--actor-type", help="human | agent | system"),
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
            "parent_id": parent_id,
            "milestone": milestone,
            "progress": progress,
            "blocked_reason": blocked_reason,
            "next_action": next_action,
            "codex_thread_id": thread_id,
            "github_issue_url": github_issue_url,
        }.items()
        if value is not None
    }
    try:
        store.update_task(
            conn, task["id"], actor_type=actor_type, actor_name=actor, source="cli", **fields
        )
    except ValueError as exc:
        _err(str(exc))
    typer.secho(f"updated task {task['id']}", fg=typer.colors.GREEN)


if __name__ == "__main__":  # pragma: no cover
    app()
