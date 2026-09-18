"""A/B a fixed task set, so Phase 4's claim can be checked rather than asserted.

The exit criterion is "lower tokens per accepted task with no drop in
acceptance". That is a comparison, and a comparison needs the same work done
twice under two named configurations, on the same starting revision, with the
result read from what actually happened rather than from what was intended.

What this harness is careful about:

* **The same tasks, not similar ones.** One task set is defined once and each
  arm gets its own copy, so arm B cannot be quietly easier.
* **Acceptance is the gate's verdict, not the exit code.** An arm that produces
  cheap changes nobody would merge has not won anything.
* **The verdict is refused when it is not supported.** A difference smaller
  than the run-to-run noise, or an arm with no accepted work at all, reports
  "not proven" rather than a percentage.

Nothing here starts work on its own: the caller supplies the runner, which is
how the tests drive it without agents and how a real comparison drives it with
them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from . import ids, store

# Below this relative difference the arms are called even: two runs of the same
# configuration vary by more than this from prompt caching alone.
NOISE_FLOOR = 0.10


@dataclass
class ArmResult:
    """What one configuration did with the task set."""

    name: str
    settings: dict[str, Any] = field(default_factory=dict)
    tasks: int = 0
    accepted: int = 0
    tokens: int = 0
    review_tokens: int = 0
    seconds: float = 0.0
    task_ids: list[str] = field(default_factory=list)

    @property
    def acceptance_rate(self) -> float | None:
        return round(self.accepted / self.tasks * 100, 1) if self.tasks else None

    @property
    def tokens_per_accepted(self) -> int | None:
        if not self.accepted:
            return None
        return round((self.tokens + self.review_tokens) / self.accepted)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "settings": self.settings, "tasks": self.tasks,
            "accepted": self.accepted, "tokens": self.tokens,
            "review_tokens": self.review_tokens,
            "total_tokens": self.tokens + self.review_tokens,
            "seconds": round(self.seconds),
            "acceptance_rate": self.acceptance_rate,
            "tokens_per_accepted": self.tokens_per_accepted,
            "task_ids": self.task_ids,
        }


@dataclass
class Comparison:
    baseline: ArmResult
    candidate: ArmResult

    @property
    def verdict(self) -> str:
        """better | worse | even | not_proven, judged on the stated criterion."""
        base, cand = self.baseline.tokens_per_accepted, self.candidate.tokens_per_accepted
        if base is None or cand is None:
            return "not_proven"
        if self.candidate.acceptance_rate is not None and (
            self.baseline.acceptance_rate is not None
            and self.candidate.acceptance_rate < self.baseline.acceptance_rate
        ):
            # Cheaper but less often accepted is not the trade the phase asked
            # for, whatever the token number says.
            return "worse"
        change = (cand - base) / base
        if abs(change) < NOISE_FLOOR:
            return "even"
        return "better" if change < 0 else "worse"

    @property
    def token_change(self) -> float | None:
        base, cand = self.baseline.tokens_per_accepted, self.candidate.tokens_per_accepted
        if base is None or cand is None or base == 0:
            return None
        return round((cand - base) / base * 100, 1)

    def summary(self) -> str:
        verdict = self.verdict
        if verdict == "not_proven":
            missing = [
                arm.name for arm in (self.baseline, self.candidate) if not arm.accepted
            ]
            return (
                "Not proven: "
                + (f"{', '.join(missing)} accepted nothing." if missing
                   else "there is not enough accepted work to compare.")
            )
        change = self.token_change
        direction = "fewer" if (change or 0) < 0 else "more"
        detail = (
            f"{abs(change)}% {direction} tokens per accepted task "
            f"({self.baseline.tokens_per_accepted} -> {self.candidate.tokens_per_accepted})"
        )
        if verdict == "even":
            return f"Even: {detail}, inside the {round(NOISE_FLOOR * 100)}% noise floor."
        if verdict == "worse" and (change or 0) < 0:
            return (
                f"Worse: {detail}, but acceptance fell from "
                f"{self.baseline.acceptance_rate}% to {self.candidate.acceptance_rate}%."
            )
        return f"{verdict.capitalize()}: {detail}."

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline.as_dict(),
            "candidate": self.candidate.as_dict(),
            "verdict": self.verdict,
            "token_change_pct": self.token_change,
            "noise_floor_pct": round(NOISE_FLOOR * 100),
            "summary": self.summary(),
        }


@dataclass(frozen=True)
class TaskSpec:
    """One piece of work in the fixed set, copied into each arm."""

    title: str
    brief: str
    acceptance: str
    allowed_paths: str | None = None
    task_type: str = "code"
    complexity: int = 5


def create_tasks(
    conn: sqlite3.Connection, project_id: str, specs: list[TaskSpec], *, arm: str
) -> list[str]:
    """One copy of the task set for this arm, tagged so the two never mix."""
    task_ids = []
    for spec in specs:
        task_ids.append(store.create_task(
            conn,
            project_id=project_id,
            title=f"[{arm}] {spec.title}",
            type=spec.task_type,
            brief=spec.brief,
            acceptance=spec.acceptance,
            allowed_paths=spec.allowed_paths,
            complexity=spec.complexity,
            risk="low",
        ))
    return task_ids


def measure(conn: sqlite3.Connection, task_ids: list[str]) -> tuple[int, int, int, float]:
    """(accepted, worker tokens, review tokens, seconds) for these tasks.

    Accepted means the gate merged the change or would have. Everything is read
    back from the runs and verifications rather than from the runner's own
    report, so a runner that lies cannot win a comparison.
    """
    from . import scoreboard

    accepted = tokens = review_tokens = 0
    seconds = 0.0
    for task_id in task_ids:
        rows = conn.execute(
            """SELECT r.id, r.started_at, r.ended_at, r.usage_json, v.status, v.checks_json
               FROM runs r LEFT JOIN verifications v ON v.run_id = r.id
               WHERE r.task_id = ?""",
            (task_id,),
        ).fetchall()
        merged = False
        for row in rows:
            usage = scoreboard._usage_totals(row["usage_json"])
            tokens += usage["input_tokens"] + usage["output_tokens"]
            span = scoreboard._seconds(row["started_at"], row["ended_at"])
            seconds += span or 0.0
            if row["status"] in {"merged", "verified"}:
                merged = True
            review_tokens += _review_tokens(row["checks_json"])
        accepted += 1 if merged else 0
    return accepted, tokens, review_tokens, seconds


def run_arm(
    conn: sqlite3.Connection,
    *,
    name: str,
    project_id: str,
    specs: list[TaskSpec],
    runner: Callable[[sqlite3.Connection, str], Any],
    settings: dict[str, Any] | None = None,
) -> ArmResult:
    """Create this arm's tasks, run each one, then measure what happened."""
    task_ids = create_tasks(conn, project_id, specs, arm=name)
    for task_id in task_ids:
        runner(conn, task_id)
    accepted, tokens, review_tokens, seconds = measure(conn, task_ids)
    return ArmResult(
        name=name, settings=settings or {}, tasks=len(task_ids), accepted=accepted,
        tokens=tokens, review_tokens=review_tokens, seconds=seconds, task_ids=task_ids,
    )


def compare(baseline: ArmResult, candidate: ArmResult) -> Comparison:
    return Comparison(baseline=baseline, candidate=candidate)


def record(
    conn: sqlite3.Connection, project_id: str, comparison: Comparison, *, note: str = ""
) -> None:
    """Keep the comparison where the owner will find it later."""
    store.create_activity_event(
        conn,
        project_id=project_id,
        actor_type="system",
        actor_name="cortex-ab",
        action="ab.compared",
        summary=comparison.summary()[:500],
        source="cortex-ab",
        evidence={"note": note, **comparison.as_dict()},
    )


def latest(conn: sqlite3.Connection, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT occurred_at, summary, evidence_json FROM activity_events
           WHERE action = 'ab.compared' ORDER BY occurred_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    results = []
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except json.JSONDecodeError:
            evidence = {}
        results.append({"at": row["occurred_at"], "summary": row["summary"], **evidence})
    return results


def _review_tokens(checks_json: str | None) -> int:
    from . import scoreboard, verification  # noqa: F401  (verification for symmetry)

    check = None
    try:
        for item in json.loads(checks_json or "[]"):
            if item.get("name") == "review":
                check = item
                break
    except json.JSONDecodeError:
        return 0
    if not check:
        return 0
    usage = scoreboard._usage_totals((check.get("evidence") or {}).get("usage"))
    return usage["input_tokens"] + usage["output_tokens"]


def now() -> str:
    return ids.now()
