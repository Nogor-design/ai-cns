"""Deterministic, explainable task routing.

Routing starts with risk and privacy, then estimates complexity. Run history can
replace these seed choices later; the rules are intentionally transparent.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass


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


@dataclass(frozen=True)
class Route:
    risk: str
    complexity: int
    worker: str
    model: str
    action: str
    budget: str
    reviewer: str | None
    requires_approval: bool
    reasons: tuple[str, ...]


def route_task(project: sqlite3.Row, task: sqlite3.Row) -> Route:
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

    if task["type"] == "research" and _CURRENT_RESEARCH.search(text):
        return Route(
            risk=risk,
            complexity=score,
            worker="perplexity",
            model="search",
            action="research",
            budget=budget,
            reviewer="codex",
            requires_approval=risk == "high",
            reasons=tuple(reasons + ["current web research needs sourced retrieval"]),
        )

    if risk == "high" or score >= 7:
        return Route(
            risk=risk,
            complexity=score,
            worker="codex",
            model="default",
            action="implement" if task["type"] == "code" else "review",
            budget=budget,
            reviewer="claude",
            requires_approval=True,
            reasons=tuple(reasons + ["high-risk/high-complexity work uses premium integration"]),
        )

    if task["type"] == "code":
        if score <= 3:
            return Route(
                risk=risk,
                complexity=score,
                worker="ollama",
                model="qwen3-coder:30b",
                action="draft",
                budget="local",
                reviewer="codex",
                requires_approval=False,
                reasons=tuple(reasons + ["bounded code draft can start locally"]),
            )
        return Route(
            risk=risk,
            complexity=score,
            worker="gemini",
            model="default",
            action="implement",
            budget=budget,
            reviewer="codex",
            requires_approval=False,
            reasons=tuple(reasons + ["medium implementation with Codex acceptance"]),
        )

    if task["type"] in {"review", "planning"} and score >= 3:
        return Route(
            risk=risk,
            complexity=score,
            worker="claude",
            model="sonnet",
            action="review",
            budget=budget,
            reviewer="codex",
            requires_approval=False,
            reasons=tuple(reasons + ["architecture/planning benefits from an independent review"]),
        )

    return Route(
        risk=risk,
        complexity=score,
        worker="ollama",
        model="phi4:14b",
        action="review",
        budget="local",
        reviewer="codex" if task["type"] == "review" else None,
        requires_approval=False,
        reasons=tuple(reasons + ["low-risk first pass stays local"]),
    )
