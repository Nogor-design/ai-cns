"""The canonical state document — `/.cortex/state.md` (spec section 5).

Regenerated, not appended: kept compact and always-current. A local Ollama
model regenerates it from the previous state, recent runs, and recent decisions.
When Ollama is unavailable, a deterministic builder produces an equivalent
compact document from the database, so the feature always works offline.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import config, ids, ollama_client, store


def scaffold(project: sqlite3.Row) -> str:
    """Initial state.md content for a freshly registered project."""
    goal = project["current_goal"] or "<one or two sentences — the current objective>"
    stack = project["stack"] or "<languages, frameworks, key services>"
    dod = (
        "- tests pass\n- app boots\n- no secrets in diff\n- client-safe data only"
    )
    return f"""# {project['name']} — State
_Last updated: {ids.today()} by Cortex_

## Goal (now)
{goal}

## Stack
{stack}

## Important files
- path — what it does

## Open tasks
- [ ] <task> (type, priority)

## Recent decisions (last ~10, newest first)
- {ids.today()} — Project registered with Cortex — initialize memory layer — source: manual

## Known risks / assumptions
- <risk or assumption>

## Definition of done (for this project)
{dod}
"""


def write_state(repo_path: str | Path, content: str) -> Path:
    path = config.state_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_initial_state(repo_path: str | Path, content: str) -> Path:
    """Create a starter state document without overwriting owner work."""
    path = config.state_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with path.open("x", encoding="utf-8") as handle:
            created = True
            handle.write(content)
    except Exception:
        if created and path.is_file():
            path.unlink()
        raise
    return path


def read_state(repo_path: str | Path) -> str | None:
    path = config.state_path(repo_path)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _deterministic(project: sqlite3.Row, conn: sqlite3.Connection, previous: str | None) -> str:
    """Build a compact state doc straight from the database (no model)."""
    pid = project["id"]
    open_tasks = store.list_tasks(conn, pid)
    active_statuses = {"open", "assigned", "in_progress", "running", "review", "blocked"}
    open_lines = []
    for task in open_tasks:
        if task["status"] not in active_statuses:
            continue
        details = [task["type"], task["status"]]
        if task["assignee"]:
            details.append(f"owner: {task['assignee']}")
        details.append(f"progress: {task['progress']}%")
        if task["next_action"]:
            details.append(f"next: {task['next_action']}")
        if task["blocked_reason"]:
            details.append(f"blocked: {task['blocked_reason']}")
        open_lines.append(f"- [ ] {task['title']} ({'; '.join(details)})")
    if not open_lines:
        open_lines = ["- [ ] <none>"]

    decisions = store.recent_decisions(conn, pid, limit=10)
    dec_lines = [
        f"- {d['ts'][:10]} — {d['decision']} — {d['rationale'] or ''} — source: {d['source']}"
        for d in decisions
    ] or ["- <none yet>"]

    activity = store.list_activity_events(conn, pid, limit=10)
    activity_lines = [
        f"- {event['occurred_at'][:16]} — {event['actor_name'] or event['actor_type']} — "
        f"{event['summary']} ({event['action']})"
        for event in activity
    ] or ["- <none yet>"]

    # Preserve human-authored sections from the previous doc where the DB has
    # nothing to say (Important files, Known risks, Definition of done).
    preserved = _extract_sections(
        previous, ["Important files", "Known risks / assumptions", "Definition of done"]
    )
    important = preserved.get("Important files") or "- path — what it does"
    risks = preserved.get("Known risks / assumptions") or "- <risk or assumption>"
    dod = preserved.get("Definition of done") or (
        "- tests pass\n- app boots\n- no secrets in diff\n- client-safe data only"
    )

    goal = project["current_goal"] or "<the current objective>"
    stack = project["stack"] or "<languages, frameworks, key services>"

    return f"""# {project['name']} — State
_Last updated: {ids.today()} by Cortex_

## Goal (now)
{goal}

## Stack
{stack}

## Important files
{important}

## Open tasks
{chr(10).join(open_lines)}

## Recent decisions (last ~10, newest first)
{chr(10).join(dec_lines)}

## Recent activity (last 10, newest first)
{chr(10).join(activity_lines)}

## Known risks / assumptions
{risks}

## Definition of done (for this project)
{dod}
"""


def _extract_sections(text: str | None, headings: list[str]) -> dict[str, str]:
    """Pull the body under each `## heading` from a state document."""
    out: dict[str, str] = {}
    if not text:
        return out
    lines = text.splitlines()
    current: str | None = None
    buf: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current in headings:
                out[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if current in headings:
        out[current] = "\n".join(buf).strip()
    return {k: v for k, v in out.items() if v}


def regen(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    *,
    use_ollama: bool = True,
) -> str:
    """Regenerate state.md from previous state + recent runs + decisions.

    Tries Ollama for a clean compact rewrite; falls back to the deterministic
    builder if the model is unavailable or returns something unusable.
    """
    repo = project["repo_path"]
    previous = read_state(repo)
    deterministic = _deterministic(project, conn, previous)

    if use_ollama and ollama_client.available():
        try:
            runs = store.list_runs(conn, project["id"], limit=10)
            content = _ollama_regen(project, previous, runs, deterministic)
            if content and content.lstrip().startswith("#"):
                write_state(repo, content)
                return content
        except ollama_client.OllamaUnavailable:
            pass

    write_state(repo, deterministic)
    return deterministic


def _ollama_regen(
    project: sqlite3.Row,
    previous: str | None,
    runs: list[sqlite3.Row],
    deterministic: str,
) -> str:
    run_summary = "\n".join(
        f"- {r['started_at'][:10]} {r['model'] or '?'} ({r['execution_mode']}): "
        f"{(json.loads(r['files_changed']) if r['files_changed'] else [])} "
        f"tests={r['tests_passed']} outcome={r['outcome']}"
        for r in runs
    ) or "(no runs yet)"

    prompt = f"""You maintain a compact, always-current project STATE document.
Rewrite it fresh (do not append). Keep it tight; never let it grow unbounded.
Preserve the exact section headings of the template. Resolve conflicts rather
than accumulating them.

TEMPLATE (authoritative structure — fill it, keep headings):
{deterministic}

PREVIOUS STATE (may contain human-authored detail worth keeping):
{previous or '(none)'}

RECENT RUNS:
{run_summary}

Output ONLY the regenerated markdown document, starting with '# '."""
    return ollama_client.generate(prompt, system="Be terse and accurate.").strip()
