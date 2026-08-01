"""Low-friction AI project manager: inspect, suggest, approve, continue.

Planning is read-only and cached. A proposal is not a task until the owner
approves it. If a requested worker is unavailable (or its output is invalid),
Cortex still returns a useful deterministic plan so the button never dead-ends.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import evidence, ids, routing, secrets_scan, store, workers


class PlanningError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlanResult:
    project_id: str
    suggestion_ids: tuple[str, ...]
    source_worker: str
    source_model: str | None
    cached: bool = False
    note: str | None = None


def plan_project(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    *,
    worker: str = "codex",
    model: str = "default",
    count: int = 3,
    force: bool = False,
    strict: bool = False,
    allow_cloud: bool = False,
    timeout: int = 600,
) -> PlanResult:
    """Create 2-3 bounded recommendations, reusing a current plan by default."""
    count = max(1, min(3, int(count)))
    current = store.list_suggestions(conn, project["id"], status="proposed")
    if current and not force:
        return PlanResult(
            project_id=project["id"],
            suggestion_ids=tuple(row["id"] for row in current[:count]),
            source_worker=current[0]["source_worker"],
            source_model=current[0]["source_model"],
            cached=True,
            note="Reused the current plan to avoid spending tokens twice.",
        )
    if force:
        for row in current:
            store.update_suggestion(conn, row["id"], status="dismissed")

    proposals: list[dict[str, Any]] | None = None
    note: str | None = None
    source_worker = worker
    source_model: str | None = model
    if project["privacy"] == "restricted" and worker != "ollama" and not allow_cloud:
        note = "Restricted project: used the no-cloud planner."
    elif workers.available(worker):
        try:
            proposals = _ask_worker(
                conn, project, worker=worker, model=model, count=count, timeout=timeout
            )
        except (PlanningError, workers.WorkerError) as exc:
            if strict:
                raise PlanningError(str(exc)) from exc
            note = f"{worker.title()} planner was unavailable; used the no-token fallback."
    else:
        note = f"{worker.title()} CLI was not available; used the no-token fallback."

    if not proposals:
        proposals = _deterministic_proposals(project, count=count)
        source_worker = "deterministic"
        source_model = None

    suggestion_ids: list[str] = []
    for proposal in proposals[:count]:
        item = _normalise(proposal)
        route = routing.route_task(project, item)
        suggestion_ids.append(
            store.create_suggestion(
                conn,
                project_id=project["id"],
                title=item["title"],
                type=item["type"],
                why=item["why"],
                brief=item["brief"],
                risk=item["risk"],
                complexity=item["complexity"],
                acceptance=item["acceptance"],
                allowed_paths=item["allowed_paths"],
                budget=item["budget"] or route.budget,
                priority=item["priority"],
                recommended_worker=route.worker,
                recommended_model=route.model,
                action=route.action,
                reviewer=route.reviewer,
                requires_approval=route.requires_approval,
                source_worker=source_worker,
                source_model=source_model,
            )
        )
    return PlanResult(
        project_id=project["id"],
        suggestion_ids=tuple(suggestion_ids),
        source_worker=source_worker,
        source_model=source_model,
        note=note,
    )


def portfolio_focus(
    conn: sqlite3.Connection, project_id: str | None = None
) -> dict[str, Any]:
    """Choose the next screen/action without consuming an AI token."""
    projects = {
        row["id"]: row
        for row in store.list_projects(conn)
        if row["status"] == "active" and (not project_id or row["id"] == project_id)
    }
    tasks = [
        row for row in store.list_all_tasks(conn)
        if row["project_id"] in projects
    ]
    suggestions = [
        row for row in store.list_suggestions(conn, status="proposed")
        if row["project_id"] in projects
    ]
    decisions = [
        row for row in tasks
        if row["status"] in {"review", "blocked"}
        or str(row["assignee"] or "").lower() in {"owner", "perplexity"}
    ]
    working = [
        row for row in tasks
        if row["status"] in {"running", "in_progress", "assigned"}
        and str(row["assignee"] or "").lower() not in {"owner", "perplexity"}
    ]
    ready = [row for row in tasks if row["status"] == "open"]
    if decisions:
        row = decisions[0]
        focus = {"kind": "decision", "id": row["id"], "project_id": row["project_id"]}
    elif working:
        row = working[0]
        focus = {"kind": "work", "id": row["id"], "project_id": row["project_id"]}
    elif ready:
        row = ready[0]
        focus = {"kind": "task", "id": row["id"], "project_id": row["project_id"]}
    elif suggestions:
        row = suggestions[0]
        focus = {"kind": "suggestion", "id": row["id"], "project_id": row["project_id"]}
    else:
        project = sorted(projects.values(), key=lambda row: (row["priority"], row["updated_at"]))[0] if projects else None
        focus = {"kind": "plan", "project_id": project["id"]} if project else {"kind": "empty"}
    return {
        "focus": focus,
        "needs_decision_ids": [row["id"] for row in decisions],
        "working_ids": [row["id"] for row in working],
        "ready_task_ids": [row["id"] for row in ready],
        "suggestion_ids": [row["id"] for row in suggestions],
    }


def _ask_worker(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    *,
    worker: str,
    model: str,
    count: int,
    timeout: int,
) -> list[dict[str, Any]]:
    prompt = _planning_prompt(conn, project, worker=worker, count=count)
    findings = secrets_scan.scan(prompt, use_ollama=False)
    if findings:
        raise PlanningError("project context was blocked by the local privacy scan")
    spec = workers.build_command(
        worker=worker,
        model=model,
        action="review",
        workspace=project["repo_path"],
        brief_path=Path(project["repo_path"]) / ".cortex" / "pm-plan.md",
        budget="small",
    )
    if worker == "codex":
        # PM planning does not need the user's interactive plugin/MCP stack.
        # Keeping the process ephemeral lowers startup noise and prevents an
        # unrelated plugin configuration problem from blocking a project plan.
        argv = list(spec.argv)
        argv[2:2] = ["--ignore-user-config", "--ephemeral", "--disable", "plugins"]
        spec = workers.CommandSpec(spec.worker, tuple(argv), spec.uses_stdin)
    result = workers.execute(
        spec, workspace=project["repo_path"], brief=prompt, timeout=timeout
    )
    if result.exit_code != 0:
        raise PlanningError(result.stderr[-800:] or f"{worker} exited with {result.exit_code}")
    return _parse_proposals(result.stdout)


def _planning_prompt(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    *,
    worker: str,
    count: int,
) -> str:
    open_tasks = store.list_tasks(conn, project["id"])
    task_lines = [
        f"- [{row['status']}] {row['title']} (type={row['type']}, assignee={row['assignee'] or 'none'})"
        for row in open_tasks
        if row["status"] not in {"done", "abandoned"}
    ][:20]
    decisions = store.recent_decisions(conn, project["id"], limit=6)
    decision_lines = [f"- {row['decision']}: {row['rationale'] or ''}" for row in decisions]
    repo_evidence = ""
    if worker == "ollama":
        repo_evidence = evidence.bundle(project["repo_path"], max_chars=22_000)
    return f"""You are the read-only project manager for one local software project.
Inspect the repository only as needed. Do not edit files, run destructive commands,
contact external services, or start implementation. Propose exactly {count} distinct,
bounded next moves that advance the stated goal. Do not duplicate an open task.
Prefer a verifiable outcome that one AI worker can finish in one run. Keep context and
token use small. Return ONLY a JSON array, with no Markdown, using this schema:
[{{"title":"...","type":"code|review|research|docs|data|planning|other",
"why":"one sentence","brief":"2-4 bounded sentences","acceptance":"observable done condition",
"risk":"auto|low|medium|high","complexity":0,"budget":"local|small|medium|large|null",
"priority":1,"allowed_paths":["optional/glob/**"]}}]

Project: {project['name']} ({project['id']})
Repository: {project['repo_path']}
Stack: {project['stack'] or 'unknown'}
Privacy: {project['privacy']}
Goal: {project['current_goal'] or 'Clarify the next valuable milestone'}
Open work:
{chr(10).join(task_lines) or '- none'}
Recent decisions:
{chr(10).join(decision_lines) or '- none'}

{repo_evidence}""".strip()


def _parse_proposals(output: str) -> list[dict[str, Any]]:
    candidates: list[str] = [output]
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            item = event.get("item")
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                candidates.append(item["text"])
            for key in ("response", "result", "text", "content"):
                if isinstance(event.get(key), str):
                    candidates.append(event[key])
    for candidate in reversed(candidates):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate.strip(), flags=re.I)
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\[[\s\S]*\]", cleaned)
            if not match:
                continue
            try:
                value = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            return value
    raise PlanningError("planner did not return a valid JSON recommendation array")


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    title = str(raw.get("title") or "Review the next project milestone").strip()[:180]
    task_type = str(raw.get("type") or "planning").lower()
    if task_type not in store.TASK_TYPES:
        task_type = "other"
    risk = str(raw.get("risk") or "auto").lower()
    if risk not in store.TASK_RISKS:
        risk = "auto"
    try:
        complexity = max(0, min(10, int(raw.get("complexity", 3))))
    except (TypeError, ValueError):
        complexity = 3
    try:
        priority = max(1, min(5, int(raw.get("priority", 3))))
    except (TypeError, ValueError):
        priority = 3
    paths = raw.get("allowed_paths")
    if isinstance(paths, list):
        allowed_paths = json.dumps([str(path) for path in paths[:20]])
    elif paths:
        allowed_paths = str(paths)
    else:
        allowed_paths = None
    budget = str(raw.get("budget") or "").lower() or None
    if budget not in {None, "local", "small", "medium", "large"}:
        budget = None
    return {
        "title": title,
        "type": task_type,
        "why": str(raw.get("why") or "Moves the current goal forward with a bounded outcome.").strip()[:500],
        "brief": str(raw.get("brief") or title).strip()[:4000],
        "risk": risk,
        "complexity": complexity,
        "acceptance": str(raw.get("acceptance") or f"A reviewable result for: {title}").strip()[:1200],
        "allowed_paths": allowed_paths,
        "budget": budget,
        "priority": priority,
    }


def _deterministic_proposals(project: sqlite3.Row, *, count: int) -> list[dict[str, Any]]:
    goal = project["current_goal"] or "the next valuable milestone"
    proposals = [
        {
            "title": f"Confirm the smallest milestone for {goal}",
            "type": "planning",
            "why": "Turns the current goal into one short, verifiable unit of work.",
            "brief": "Inspect current project state and define the smallest milestone that materially advances the goal. Record scope, dependencies, and explicit non-goals; do not implement it.",
            "acceptance": "One bounded milestone with acceptance checks and non-goals is documented.",
            "risk": "low", "complexity": 2, "budget": "local", "priority": 1,
        },
        {
            "title": "Review current implementation against the project goal",
            "type": "review",
            "why": "Finds the highest-impact gap before spending tokens on implementation.",
            "brief": "Read the project status and relevant implementation. Identify the three most important gaps, with evidence, and recommend which one to address first.",
            "acceptance": "A ranked, evidence-backed gap list and one recommended next action are produced.",
            "risk": "low", "complexity": 3, "budget": "small", "priority": 2,
        },
        {
            "title": "Implement one verified step toward the current goal",
            "type": "code",
            "why": "Converts the agreed direction into measurable progress without widening scope.",
            "brief": "Implement only the smallest independently testable step toward the current goal. Preserve existing behavior outside that step and run the project's checks.",
            "acceptance": "The bounded change is complete, tests pass, and the diff is ready for review.",
            "risk": "auto", "complexity": 4, "budget": "small", "priority": 3,
        },
    ]
    return proposals[:count]
