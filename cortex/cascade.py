"""Start cheap, escalate on failure: the cascade that follows the gate.

The Phase 3 gate answers "is this change good enough". That answer is what
makes starting with a cheap worker safe: if the cheap attempt is wrong, the
gate catches it and the task moves up to a stronger worker instead of to the
owner. Phase 4's efficiency claim rests on the cheap attempt being free to
fail.

What this module will and will not do:

* It escalates only a **rejected** gate. A `needs_owner` result -- a protected
  file, a merge conflict -- is a question for the owner, and asking a more
  expensive model the same question is not an answer.
* It escalates at most :data:`MAX_ESCALATIONS` times per task, never to a
  worker that already attempted it, and never past the strongest rung. A task
  that defeats the ladder belongs in the inbox, not in a loop.
* It keeps no counter. Which workers have tried a task is read back from the
  runs that exist, so a crash, a restart or a hand-run dispatch cannot leave
  the ladder out of step with what actually happened.

Admission is not this module's job: the escalated worker still faces the same
policy, quota, lane and trading checks as any other start.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import ids, policy, routing, store

# Weakest to strongest, by what they cost and what they can be trusted with.
# Ordering is a judgement about capability, not a measurement: the scoreboard
# decides who starts, this decides who is asked next when that fails.
STRENGTH: tuple[str, ...] = (
    "ollama", "opencode-local", "opencode", "gemini", "grok", "agy", "claude", "codex",
)

# Two stronger attempts after the first. A third has never been the difference
# between working and not; it is the difference between a bounded cost and an
# unbounded one.
MAX_ESCALATIONS = 2

# Gate outcomes that mean "the work was not good enough", as opposed to "the
# owner has to decide something".
ESCALATABLE = frozenset({"rejected", "error"})


@dataclass(frozen=True)
class Rung:
    worker: str
    model: str
    reason: str
    attempt: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker": self.worker, "model": self.model,
            "reason": self.reason, "attempt": self.attempt,
        }


def attempted_workers(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Workers that have already run this task, oldest first.

    Read from the runs themselves rather than a stored attempt count, so the
    ladder always reflects what happened even after a crash or a hand dispatch.
    """
    rows = conn.execute(
        "SELECT model FROM runs WHERE task_id = ? ORDER BY started_at", (task_id,)
    ).fetchall()
    seen: list[str] = []
    for row in rows:
        worker = str(row["model"] or "").partition(":")[0].lower()
        if worker and worker not in seen:
            seen.append(worker)
    return seen


def should_escalate(gate_status: str | None) -> bool:
    """Whether this gate result is the kind another model could fix."""
    return (gate_status or "") in ESCALATABLE


def next_rung(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    task: sqlite3.Row,
    *,
    after: str,
    reason: str = "",
) -> Rung | None:
    """The next worker to try after ``after`` failed, or None to stop.

    None means the ladder is finished -- exhausted, capped, or the project
    allows nothing stronger -- and the task belongs to the owner now.
    """
    attempted = attempted_workers(conn, task["id"])
    escalations = max(0, len(attempted) - 1)
    if escalations >= MAX_ESCALATIONS:
        return None

    try:
        floor = STRENGTH.index(after.lower())
    except ValueError:
        # An unknown worker (a legacy row, a renamed CLI) is treated as the
        # weakest thing tried, so the ladder still has somewhere to go.
        floor = -1
    for worker in STRENGTH[floor + 1:]:
        if worker in attempted or not policy.is_allowed(project, worker):
            continue
        return Rung(
            worker=worker,
            model=routing.DEFAULT_MODELS.get(worker, "default"),
            reason=reason or f"{after} did not pass the verification gate",
            attempt=escalations + 2,
        )
    return None


def escalate(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    task: sqlite3.Row,
    *,
    after: str,
    gate_status: str | None,
    reason: str = "",
    run_id: str | None = None,
) -> Rung | None:
    """Hand a rejected task to a stronger worker, or leave it for the owner.

    Returns the rung that was assigned, or None when nothing was. Assigning it
    only queues the work: every admission check still runs when it starts.
    """
    if not should_escalate(gate_status):
        return None
    rung = next_rung(conn, project, task, after=after, reason=reason)
    if rung is None:
        return None
    store.update_task(conn, task["id"], status="assigned", assignee=rung.worker)
    store.create_activity_event(
        conn,
        project_id=project["id"],
        task_id=task["id"],
        actor_type="system",
        actor_name="cortex-cascade",
        model=f"{rung.worker}:{rung.model}",
        action="task.escalated",
        summary=(
            f"{after} was rejected; attempt {rung.attempt} goes to {rung.worker}"
        )[:500],
        source="cortex-cascade",
        source_ref=run_id,
        evidence={"after": after, "reason": rung.reason, **rung.as_dict()},
    )
    return rung


def history(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """Each attempt at this task and what the gate made of it."""
    rows = conn.execute(
        """SELECT r.id, r.model, r.started_at, r.exit_code, v.status AS gate_status,
                  v.merge_commit
           FROM runs r
           LEFT JOIN verifications v ON v.run_id = r.id
           WHERE r.task_id = ?
           ORDER BY r.started_at""",
        (task_id,),
    ).fetchall()
    return [
        {
            "run_id": row["id"],
            "worker": str(row["model"] or "").partition(":")[0].lower() or None,
            "model": str(row["model"] or "").partition(":")[2] or None,
            "started_at": row["started_at"],
            "exit_code": row["exit_code"],
            "gate_status": row["gate_status"],
            "merge_commit": row["merge_commit"],
        }
        for row in rows
    ]


def payload(conn: sqlite3.Connection, *, limit: int = 20) -> dict[str, Any]:
    """Recent escalations, for the dashboard and for tuning the ladder."""
    rows = conn.execute(
        """SELECT e.occurred_at, e.summary, e.model, e.evidence_json, t.title,
                  p.name AS project_name
           FROM activity_events e
           LEFT JOIN tasks t ON t.id = e.task_id
           LEFT JOIN projects p ON p.id = e.project_id
           WHERE e.action = 'task.escalated'
           ORDER BY e.occurred_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return {
        "generated_at": ids.now(),
        "ladder": list(STRENGTH),
        "max_escalations": MAX_ESCALATIONS,
        "recent": [
            {
                "at": row["occurred_at"], "summary": row["summary"],
                "worker": str(row["model"] or "").partition(":")[0],
                "task": row["title"], "project": row["project_name"],
            }
            for row in rows
        ],
    }
