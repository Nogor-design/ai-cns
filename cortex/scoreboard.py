"""What each worker actually costs per unit of accepted work.

Phase 4 begins here, with measurement only. Routing today is rules over task
text; the scoreboard is the evidence that would justify changing those rules,
and it is deliberately built before any ranking function so the weights can be
argued about against real numbers rather than invented alongside them.

Three honesty rules shape it:

* **Accepted and gate-passed are different measures.** A change that passed the
  Phase 3 gate merged into an integration branch; it was not accepted by the
  owner. Both are reported, never averaged into one "quality" number.
* **Tokens per accepted task is undefined when nothing was accepted.** A worker
  with 40k tokens spent and no accepted work is not infinitely expensive, it is
  unproven, and the row says so rather than printing a number.
* **Sample size travels with every rate.** A 100% acceptance rate over one run
  is not a fact about a worker. Rows carry ``attempts`` and a ``proven`` flag,
  and anything that ranks on this later has to look at them.

The reviewer's own tokens are counted separately, as the overhead the gate
costs, because they are spent on someone else's work.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import ids, team

# Below this many completed runs, a rate is an anecdote. Chosen to be small
# enough to say something on a young portfolio and large enough that one lucky
# run cannot top the table.
PROVEN_AFTER = 5

ACCEPTED_OUTCOMES = ("accepted", "survived")


@dataclass
class Cell:
    """One (worker, model, task type) triple's record."""

    worker: str
    model: str
    task_type: str
    attempts: int = 0
    completed: int = 0
    succeeded: int = 0          # exited 0
    accepted: int = 0           # the owner kept it (git or explicit acceptance)
    gate_judged: int = 0        # runs the Phase 3 gate judged
    gate_passed: int = 0        # ... and merged or would have merged
    review_judged: int = 0      # runs a second model actually reviewed
    review_passed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    durations: list[float] = field(default_factory=list)
    last_used: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def proven(self) -> bool:
        return self.completed >= PROVEN_AFTER

    @property
    def success_rate(self) -> float | None:
        return _rate(self.succeeded, self.completed)

    @property
    def acceptance_rate(self) -> float | None:
        return _rate(self.accepted, self.completed)

    @property
    def gate_pass_rate(self) -> float | None:
        return _rate(self.gate_passed, self.gate_judged)

    @property
    def review_pass_rate(self) -> float | None:
        return _rate(self.review_passed, self.review_judged)

    @property
    def tokens_per_accepted(self) -> int | None:
        """The number Phase 4 exists to lower. None means "not yet proven"."""
        if not self.accepted or not self.total_tokens:
            return None
        return round(self.total_tokens / self.accepted)

    @property
    def median_seconds(self) -> int | None:
        return round(statistics.median(self.durations)) if self.durations else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker": self.worker, "model": self.model, "task_type": self.task_type,
            "attempts": self.attempts, "completed": self.completed,
            "succeeded": self.succeeded, "accepted": self.accepted,
            "gate_judged": self.gate_judged, "gate_passed": self.gate_passed,
            "review_judged": self.review_judged, "review_passed": self.review_passed,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens, "total_tokens": self.total_tokens,
            "success_rate": self.success_rate, "acceptance_rate": self.acceptance_rate,
            "gate_pass_rate": self.gate_pass_rate, "review_pass_rate": self.review_pass_rate,
            "tokens_per_accepted": self.tokens_per_accepted,
            "median_seconds": self.median_seconds, "last_used": self.last_used,
            "proven": self.proven,
        }


def cells(
    conn: sqlite3.Connection, *, since_days: int | None = None, task_type: str | None = None
) -> list[Cell]:
    """One row per (worker, model, task type), newest activity first."""
    where = ["runs.started_at IS NOT NULL"]
    params: list[Any] = []
    if since_days:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=int(since_days))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        where.append("runs.started_at >= ?")
        params.append(cutoff)
    if task_type:
        where.append("tasks.type = ?")
        params.append(task_type)
    rows = conn.execute(
        f"""SELECT runs.id, runs.model, runs.started_at, runs.ended_at, runs.exit_code,
                   runs.outcome, runs.usage_json, tasks.type AS task_type
            FROM runs JOIN tasks ON tasks.id = runs.task_id
            WHERE {' AND '.join(where)}""",
        params,
    ).fetchall()
    gates = _gate_results(conn)

    grouped: dict[tuple[str, str, str], Cell] = {}
    for row in rows:
        worker, _, model = str(row["model"] or "").partition(":")
        worker = worker.lower() or "unknown"
        key = (worker, model or "default", row["task_type"] or "other")
        cell = grouped.setdefault(key, Cell(*key))
        cell.attempts += 1
        if row["ended_at"]:
            cell.completed += 1
            seconds = _seconds(row["started_at"], row["ended_at"])
            if seconds is not None:
                cell.durations.append(seconds)
        if row["exit_code"] == 0:
            cell.succeeded += 1
        if row["outcome"] in ACCEPTED_OUTCOMES:
            cell.accepted += 1
        usage = team.usage_totals([{"usage_json": row["usage_json"]}])
        cell.input_tokens += usage["input_tokens"]
        cell.output_tokens += usage["output_tokens"]
        cell.cached_tokens += usage["cached_tokens"]
        if not cell.last_used or str(row["started_at"]) > cell.last_used:
            cell.last_used = str(row["started_at"])
        gate = gates.get(row["id"])
        if gate:
            cell.gate_judged += 1
            cell.gate_passed += 1 if gate["passed"] else 0
            if gate["review"] is not None:
                cell.review_judged += 1
                cell.review_passed += 1 if gate["review"] else 0
    return sorted(
        grouped.values(), key=lambda cell: (cell.last_used or "", cell.attempts), reverse=True
    )


def review_overhead(conn: sqlite3.Connection, *, since_days: int | None = None) -> dict[str, Any]:
    """What the second-model reviews themselves cost, by reviewer.

    Kept apart from the producing worker's own usage: these tokens buy
    confidence in someone else's work, and charging them to the producer would
    make a cheap implementer look expensive for being reviewed.
    """
    params: list[Any] = []
    where = ""
    if since_days:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=int(since_days))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        where = "WHERE created_at >= ?"
        params.append(cutoff)
    reviewers: dict[str, dict[str, int]] = {}
    for row in conn.execute(f"SELECT checks_json FROM verifications {where}", params):
        check = _review_check(row["checks_json"])
        if not check:
            continue
        evidence = check.get("evidence") or {}
        reviewer = evidence.get("reviewer")
        if not reviewer:
            continue
        entry = reviewers.setdefault(
            reviewer, {"reviews": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
        )
        entry["reviews"] += 1
        usage = team.usage_totals([{"usage_json": evidence.get("usage")}])
        for key in ("input_tokens", "output_tokens", "cached_tokens"):
            entry[key] += usage[key]
    return {
        "reviewers": [{"reviewer": name, **values} for name, values in sorted(reviewers.items())],
        "total_tokens": sum(
            values["input_tokens"] + values["output_tokens"] for values in reviewers.values()
        ),
    }


def payload(
    conn: sqlite3.Connection, *, since_days: int | None = None
) -> dict[str, Any]:
    rows = cells(conn, since_days=since_days)
    proven = [cell for cell in rows if cell.proven]
    return {
        "generated_at": ids.now(),
        "since_days": since_days,
        "proven_after": PROVEN_AFTER,
        "cells": [cell.as_dict() for cell in rows],
        "review_overhead": review_overhead(conn, since_days=since_days),
        "totals": {
            "attempts": sum(cell.attempts for cell in rows),
            "accepted": sum(cell.accepted for cell in rows),
            "tokens": sum(cell.total_tokens for cell in rows),
            "proven_cells": len(proven),
        },
        # Said once, here, so every consumer repeats the same caveat.
        "note": (
            f"Rates over fewer than {PROVEN_AFTER} completed runs are anecdotes; "
            "'accepted' means the owner kept the work, which is not the same as "
            "passing the verification gate."
        ),
    }


def best_for(
    conn: sqlite3.Connection, task_type: str, *, since_days: int | None = None
) -> Cell | None:
    """The cheapest proven cell per accepted task for this kind of work.

    Deliberately not a ranking function: one measure, proven rows only, and no
    weights. The weighted ranker that replaces routing is the owner's to author
    (plan section 5, Phase 4) and should not be smuggled in as a default here.
    """
    candidates = [
        cell for cell in cells(conn, since_days=since_days, task_type=task_type)
        if cell.proven and cell.tokens_per_accepted is not None
    ]
    return min(candidates, key=lambda cell: cell.tokens_per_accepted) if candidates else None


def _gate_results(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Per run: did the gate pass it, and did a second model judge it."""
    results: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT run_id, status, checks_json FROM verifications WHERE run_id IS NOT NULL"
    ):
        check = _review_check(row["checks_json"])
        review: bool | None = None
        if check and check["status"] in {"pass", "fail"}:
            review = check["status"] == "pass"
        results[row["run_id"]] = {
            "passed": row["status"] in {"merged", "verified"},
            "review": review,
        }
    return results


def _review_check(checks_json: str | None) -> dict[str, Any] | None:
    try:
        checks = json.loads(checks_json or "[]")
    except json.JSONDecodeError:
        return None
    return next((check for check in checks if check.get("name") == "review"), None)


def _seconds(started: Any, ended: Any) -> float | None:
    try:
        start = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    seconds = (end - start).total_seconds()
    return seconds if seconds >= 0 else None


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole * 100, 1) if whole else None
