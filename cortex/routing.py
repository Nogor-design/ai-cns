"""Deterministic, explainable task routing.

Routing starts with risk and privacy, then estimates complexity. Run history can
replace these seed choices later; the rules are intentionally transparent.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, replace

from . import policy

# Each worker's default model, used when a substitution makes the routed
# model meaningless (Ollama needs a tagged local model, the cloud CLIs do not).
DEFAULT_MODELS: dict[str, str] = {
    "codex": "default",
    "claude": "sonnet",
    "gemini": "default",
    "grok": "default",
    "ollama": "phi4:14b",
    "perplexity": "search",
}


_HIGH_RISK = re.compile(
    r"\b(production|prod|deploy|payment|stripe|billing|auth|login|credential|"
    r"secret|database|migration|supabase|customer data|candidate|resume|webhook|"
    r"licen[cs]|live trading|order execution|private key)\b",
    re.IGNORECASE,
)
_COMPLEX = re.compile(
    r"\b(cross[- ]?repo|multi[- ]?repo|architecture|migration|integration|"
    r"refactor|concurrency|security|performance|root cause)\b",
    re.IGNORECASE,
)
_CURRENT_RESEARCH = re.compile(
    r"\b(latest|current|pricing|competitor|market research|law|regulation|news)\b",
    re.IGNORECASE,
)
_ADVERSARIAL = re.compile(
    r"\b(adversarial(?:ly)?|challenge|critique|counterargument|blind spots?|red[- ]team|"
    r"break the plan|devil'?s advocate|alternative hypothesis)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Route:
    risk: str
    complexity: int
    worker: str
    model: str
    action: str
    budget: str
    effort: str
    reviewer: str | None
    requires_approval: bool
    reasons: tuple[str, ...]
    # Set when the project's allowlist overrode the task-based choice, so the
    # dashboard can show both what was ideal and what will actually run.
    preferred_worker: str | None = None
    policy_note: str | None = None
    blocked_reason: str | None = None
    # The task's explicit model request, if any. Kept so that changing the
    # worker can fall back to that worker's default without discarding a
    # choice the owner actually made.
    requested_model: str | None = None


def route_task(project: sqlite3.Row, task: sqlite3.Row) -> Route:
    """Route on task characteristics, then constrain to the project's allowlist."""
    route = _route_by_task(project, task)
    requested = task["requested_model"] if "requested_model" in task.keys() else None
    if requested:
        route = replace(route, requested_model=str(requested))
    return apply_policy(project, route)


def _model_for(worker: str, route: Route) -> str:
    """The model to use once `worker` is running this route.

    Model names are worker-specific ("sonnet" means nothing to Codex), so a
    worker change resets to that worker's default unless the owner explicitly
    requested a model on the task.
    """
    return route.requested_model or DEFAULT_MODELS.get(worker, "default")


def effective_route(project: sqlite3.Row, task: sqlite3.Row) -> Route:
    """The route dispatch will actually use, absent a caller-supplied override.

    A task's stored assignee outranks the task-based recommendation but is
    still subject to project policy. Everything that needs to predict a
    dispatch -- the dashboard, the team panel, unattended starts -- goes
    through here so none of them can disagree with the dispatcher.
    """
    route = route_task(project, task)
    assignee = str(task["assignee"] or "").lower()
    if assignee not in DEFAULT_MODELS or assignee == route.worker:
        return route
    return apply_policy(
        project,
        replace(route, worker=assignee, model=_model_for(assignee, route)),
    )


def apply_policy(project: sqlite3.Row, route: Route) -> Route:
    """Force the routed worker onto the project's permitted list."""
    chosen = policy.choose_worker(project, route.worker)
    if chosen == route.worker:
        return route
    note = policy.explain(project, route.worker, chosen)
    flagged = replace(
        route,
        preferred_worker=route.worker,
        policy_note=note,
        reasons=route.reasons + ((note,) if note else ()),
    )
    if chosen is None:
        return replace(flagged, blocked_reason=note)
    return replace(flagged, worker=chosen, model=_model_for(chosen, route))


def _route_by_task(project: sqlite3.Row, task: sqlite3.Row) -> Route:
    text = " ".join(
        value
        for value in (task["title"], task["brief"] or "", task["acceptance"] or "")
        if value
    )
    reasons: list[str] = []

    risk = task["risk"] or "auto"
    if risk == "auto":
        if project["privacy"] == "restricted" or _HIGH_RISK.search(text):
            risk = "high"
            reasons.append("restricted project or high-risk domain")
        elif task["type"] in {"code", "data"}:
            risk = "medium"
            reasons.append("code/data changes default to medium risk")
        else:
            risk = "low"
            reasons.append("read-only or low-blast-radius task")
    else:
        reasons.append("explicit task risk")

    if task["complexity"] is not None:
        score = int(task["complexity"])
        reasons.append("explicit complexity override")
    else:
        score = 0
        if task["type"] == "code":
            score += 3
        elif task["type"] in {"data", "research", "review", "planning"}:
            score += 1
        if risk == "medium":
            score += 2
        elif risk == "high":
            score += 4
        if _COMPLEX.search(text):
            score += 2
            reasons.append("cross-cutting or architectural language")
        if not task["acceptance"]:
            score += 1
            reasons.append("no explicit acceptance check")
        score = min(10, score)

    budget = task["budget"] or (
        "local" if score <= 2 else "small" if score <= 4 else "medium" if score <= 7 else "large"
    )
    requested_effort = task["effort"] if "effort" in task.keys() else None
    effort = requested_effort or (
        "low" if score <= 2 else "medium" if score <= 5 else "high" if score <= 7 else "xhigh"
    )
    requested_model = task["requested_model"] if "requested_model" in task.keys() else None

    if task["type"] == "research" and _CURRENT_RESEARCH.search(text):
        return Route(
            risk=risk,
            complexity=score,
            worker="perplexity",
            model=requested_model or "search",
            action="research",
            budget=budget,
            effort=effort,
            reviewer="codex",
            requires_approval=risk == "high",
            reasons=tuple(reasons + ["current web research needs sourced retrieval"]),
        )

    if risk == "high" or score >= 7:
        return Route(
            risk=risk,
            complexity=score,
            worker="codex",
            model=requested_model or "default",
            action="implement" if task["type"] == "code" else "review",
            budget=budget,
            effort=effort,
            reviewer="claude",
            requires_approval=True,
            reasons=tuple(reasons + ["high-risk/high-complexity work uses premium integration"]),
        )

    if _ADVERSARIAL.search(text) and task["type"] in {"review", "research", "planning"}:
        return Route(
            risk=risk,
            complexity=score,
            worker="grok",
            model=requested_model or "default",
            action="review",
            budget=budget,
            effort=effort,
            reviewer="codex",
            requires_approval=False,
            reasons=tuple(reasons + ["adversarial critique is assigned to Grok's specialist role"]),
        )

    if task["type"] == "code":
        if score <= 3:
            return Route(
                risk=risk,
                complexity=score,
                worker="ollama",
                model=requested_model or "qwen3-coder:30b",
                action="draft",
                budget="local",
                effort=effort,
                reviewer="codex",
                requires_approval=False,
                reasons=tuple(reasons + ["bounded code draft can start locally"]),
            )
        return Route(
            risk=risk,
            complexity=score,
            worker="gemini",
            model=requested_model or "default",
            action="implement",
            budget=budget,
            effort=effort,
            reviewer="codex",
            requires_approval=False,
            reasons=tuple(reasons + ["medium implementation with Codex acceptance"]),
        )

    if task["type"] in {"review", "planning"} and score >= 3:
        return Route(
            risk=risk,
            complexity=score,
            worker="claude",
            model=requested_model or "sonnet",
            action="review",
            budget=budget,
            effort=effort,
            reviewer="codex",
            requires_approval=False,
            reasons=tuple(reasons + ["architecture/planning benefits from an independent review"]),
        )

    return Route(
        risk=risk,
        complexity=score,
        worker="ollama",
        model=requested_model or "phi4:14b",
        action="review",
        budget="local",
        effort=effort,
        reviewer="codex" if task["type"] == "review" else None,
        requires_approval=False,
        reasons=tuple(reasons + ["low-risk first pass stays local"]),
    )
