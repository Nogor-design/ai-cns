"""Treat CLI workers as a visible team of specialists with evidence-backed status."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from . import policy, routing, store, workers


EXPERTS: dict[str, dict[str, Any]] = {
    "codex": {
        "role": "Lead engineer & integrator",
        "best_for": "complex code, debugging, high-risk integration, acceptance",
        "models": ["default"],
        "efforts": ["low", "medium", "high", "xhigh"],
    },
    "claude": {
        "role": "Architect & independent reviewer",
        "best_for": "architecture, plans, specifications, careful review",
        "models": ["default", "sonnet", "opus", "fable"],
        "efforts": ["low", "medium", "high", "xhigh"],
    },
    "gemini": {
        "role": "Implementation & large-context specialist",
        "best_for": "medium code changes, large repositories, broad synthesis",
        "models": ["default"],
        "efforts": [],
    },
    "grok": {
        "role": "Adversarial critic",
        "best_for": "blind spots, counterarguments, red-team review",
        "models": ["default", "grok-4.5"],
        "efforts": ["low", "medium", "high"],
    },
    "ollama": {
        "role": "Private local analyst",
        "best_for": "restricted context, triage, inexpensive first passes",
        "models": ["phi4:14b", "qwen3-coder:30b"],
        "efforts": [],
    },
    "perplexity": {
        "role": "Sourced research specialist",
        "best_for": "current facts, market research, citations",
        "models": ["search"],
        "efforts": [],
    },
}


def team_payload(
    conn: sqlite3.Connection,
    tasks: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, expert in EXPERTS.items():
        probe = workers.probe_cached(name)
        worker_tasks = [
            task for task in tasks
            if str(task.get("assignee") or task.get("recommended_worker") or "").lower() == name
        ]
        running_tasks = [task for task in worker_tasks if task["status"] == "running"]
        queued_tasks = [task for task in worker_tasks if task["status"] == "assigned"]
        worker_runs = [run for run in runs if _run_worker(run) == name]
        completed = [run for run in worker_runs if run.get("ended_at")]
        successful = [run for run in completed if run.get("exit_code") == 0]
        accepted = [run for run in completed if run.get("outcome") in {"accepted", "survived"}]
        tokens = _sum_usage(worker_runs)
        if probe["availability"] != "ready":
            state = probe["availability"]
        elif running_tasks:
            state = "working"
        elif queued_tasks:
            state = "queued"
        else:
            state = "idle"
        current = running_tasks[0] if running_tasks else queued_tasks[0] if queued_tasks else None
        rows.append({
            "name": name,
            **expert,
            "availability": probe["availability"],
            "probe_note": probe.get("note"),
            "state": state,
            "current_task": current,
            "queued": len(queued_tasks),
            "running": len(running_tasks),
            "attempts": len(worker_runs),
            "completed": len(completed),
            "successful": len(successful),
            "accepted": len(accepted),
            "success_rate": round(len(successful) / len(completed) * 100) if completed else None,
            "acceptance_rate": round(len(accepted) / len(completed) * 100) if completed else None,
            "tokens": tokens,
            "models_seen": sorted({str(run.get("model") or "").partition(":")[2] for run in worker_runs if run.get("model")}),
            "efforts_seen": sorted({str(run.get("effort")) for run in worker_runs if run.get("effort")}),
            "average_seconds": _average_seconds(completed),
        })
    return rows


def safe_start_candidates(
    conn: sqlite3.Connection, *, limit: int = 3
) -> list[str]:
    """One already-approved read-only task per available, non-running worker."""
    projects = {row["id"]: row for row in store.list_projects(conn)}
    all_tasks = store.list_all_tasks(conn)
    limit = max(1, min(6, limit))

    # Group assigned work by the worker that would actually run it, not by the
    # stored assignee. A task assigned to Codex on a Claude-only project is
    # dispatchable as Claude, and grouping by assignee would hide it forever.
    by_worker: dict[str, list[str]] = {}
    running_workers: set[str] = set()
    for row in all_tasks:
        project = projects[row["project_id"]]
        if row["status"] == "running":
            running_workers.add(routing.effective_route(project, row).worker)
            continue
        if row["status"] != "assigned" or project["status"] != "active":
            continue
        route = routing.effective_route(project, row)
        # Only writes are withheld from an unattended start; they need an
        # explicit allow_write and an isolated worktree. Read-only work is
        # gated purely by whether this project permits this worker.
        if route.action == "implement" or route.blocked_reason:
            continue
        if not policy.is_allowed(project, route.worker):
            continue
        by_worker.setdefault(route.worker, []).append(row["id"])

    selected: list[str] = []
    for worker in EXPERTS:
        if worker == "perplexity" or worker in running_workers:
            continue
        queue = by_worker.get(worker)
        if not queue:
            continue
        if workers.probe(worker)["availability"] != "ready":
            continue
        selected.append(queue[0])
        if len(selected) >= limit:
            break
    return selected


def usage_totals(runs: list[dict[str, Any]]) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0}
    for run in runs:
        usage = run.get("usage_json")
        if not usage:
            continue
        try:
            data = json.loads(usage) if isinstance(usage, str) else usage
        except json.JSONDecodeError:
            continue
        flat = _flatten_numbers(data)
        for key, value in flat.items():
            lower = key.lower()
            if "cached" in lower and "token" in lower:
                totals["cached_tokens"] += value
            elif "reasoning" in lower and "token" in lower:
                totals["reasoning_tokens"] += value
            elif ("input" in lower or "prompt" in lower) and "token" in lower:
                totals["input_tokens"] += value
            elif ("output" in lower or "completion" in lower) and "token" in lower:
                totals["output_tokens"] += value
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    return totals


def performance_payload(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Aggregate actual outcomes by worker/model, effort, and task type."""
    rows = conn.execute(
        """SELECT runs.model, runs.effort, tasks.type,
                  COUNT(*) AS attempts,
                  SUM(CASE WHEN runs.ended_at IS NOT NULL THEN 1 ELSE 0 END) AS completed,
                  SUM(CASE WHEN runs.exit_code = 0 THEN 1 ELSE 0 END) AS successful,
                  SUM(CASE WHEN runs.outcome IN ('accepted', 'survived') THEN 1 ELSE 0 END) AS accepted
           FROM runs
           JOIN tasks ON tasks.id = runs.task_id
           GROUP BY runs.model, runs.effort, tasks.type
           ORDER BY attempts DESC, runs.model, tasks.type"""
    ).fetchall()
    return [
        {
            "model": row["model"],
            "worker": str(row["model"] or "").partition(":")[0],
            "effort": row["effort"],
            "task_type": row["type"],
            "attempts": row["attempts"],
            "completed": row["completed"],
            "successful": row["successful"],
            "accepted": row["accepted"],
            "success_rate": round(row["successful"] / row["completed"] * 100)
            if row["completed"] else None,
        }
        for row in rows
    ]


def _sum_usage(runs: list[dict[str, Any]]) -> int:
    return usage_totals(runs)["total_tokens"]


def _flatten_numbers(value: Any, prefix: str = "") -> dict[str, int]:
    result: dict[str, int] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            result.update(_flatten_numbers(child, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        result[prefix] = int(value)
    return result


def _run_worker(run: dict[str, Any]) -> str:
    return str(run.get("model") or "").partition(":")[0].lower()


def _average_seconds(runs: list[dict[str, Any]]) -> int | None:
    values: list[float] = []
    for run in runs:
        try:
            start = datetime.fromisoformat(str(run["started_at"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(run["ended_at"]).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        values.append((end - start).total_seconds())
    return round(sum(values) / len(values)) if values else None
