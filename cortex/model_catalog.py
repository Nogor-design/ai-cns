"""Which cloud model and reasoning level each worker uses by default.

Routing picks a *worker* from the task; this picks the *model and level* that
worker runs at. The owner sets it once in the dashboard instead of per task.

The Codex catalog is read from Codex's own model cache so new models appear
without a code change; Claude's aliases come from its documented set. A task's
own `requested_model` / `effort` still wins, and "auto" keeps the previous
behaviour (Codex's configured default, and a level derived from complexity).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from . import settings

AUTO = "auto"
MODEL_KEY = "worker.{worker}.model"
EFFORT_KEY = "worker.{worker}.effort"
CODEX_MODELS_ENV = "CORTEX_CODEX_MODELS"
SELECTABLE = ("codex", "claude")

# Used when Codex's cache is missing (fresh machine, or a CLI that moved it).
_CODEX_FALLBACK: tuple[dict[str, Any], ...] = (
    {"slug": "gpt-6-astra", "label": "GPT-6-Astra",
     "efforts": ("low", "medium", "high", "xhigh", "max", "ultra")},
    {"slug": "gpt-5.6-sol", "label": "GPT-5.6-Sol",
     "efforts": ("low", "medium", "high", "xhigh", "max", "ultra")},
    {"slug": "gpt-5.6-terra", "label": "GPT-5.6-Terra",
     "efforts": ("low", "medium", "high", "xhigh", "max", "ultra")},
    {"slug": "gpt-5.6-luna", "label": "GPT-5.6-Luna",
     "efforts": ("low", "medium", "high", "xhigh", "max")},
)

# Claude's CLI takes an alias for the latest model of each family, plus a
# session effort level.
_CLAUDE_MODELS: tuple[dict[str, Any], ...] = (
    {"slug": "opus", "label": "Claude Opus", "efforts": ("low", "medium", "high", "xhigh", "max")},
    {"slug": "sonnet", "label": "Claude Sonnet", "efforts": ("low", "medium", "high", "xhigh", "max")},
    {"slug": "haiku", "label": "Claude Haiku", "efforts": ("low", "medium", "high")},
    {"slug": "fable", "label": "Claude Fable", "efforts": ("low", "medium", "high", "xhigh", "max")},
)


def codex_models_path() -> Path:
    override = os.environ.get(CODEX_MODELS_ENV)
    if override:
        return Path(override)
    return Path.home() / ".codex" / "models_cache.json"


def codex_models() -> list[dict[str, Any]]:
    """Models Codex offers, newest-first as Codex itself orders them."""
    try:
        document = json.loads(codex_models_path().read_text(encoding="utf-8"))
        entries = document["models"]
    except (OSError, ValueError, KeyError, TypeError):
        return [dict(model) for model in _CODEX_FALLBACK]
    catalog: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("visibility") != "list":
            continue  # hidden entries are internal (auto-review, reserve)
        efforts = tuple(
            str(level.get("effort"))
            for level in entry.get("supported_reasoning_levels") or []
            if isinstance(level, dict) and level.get("effort")
        )
        catalog.append({
            "slug": str(entry.get("slug")),
            "label": str(entry.get("display_name") or entry.get("slug")),
            "efforts": efforts or ("low", "medium", "high"),
            "default_effort": entry.get("default_reasoning_level"),
            "description": entry.get("description"),
        })
    return catalog or [dict(model) for model in _CODEX_FALLBACK]


def models_for(worker: str) -> list[dict[str, Any]]:
    if worker == "codex":
        return codex_models()
    if worker == "claude":
        return [dict(model) for model in _CLAUDE_MODELS]
    return []


def efforts_for(worker: str, model: str) -> tuple[str, ...]:
    for entry in models_for(worker):
        if entry["slug"] == model:
            return tuple(entry["efforts"])
    return ("low", "medium", "high", "xhigh")


def choice(conn: sqlite3.Connection, worker: str) -> dict[str, str]:
    """The owner's model and level for ``worker``; ``auto`` means unchanged."""
    return {
        "model": settings.get(conn, MODEL_KEY.format(worker=worker)) or AUTO,
        "effort": settings.get(conn, EFFORT_KEY.format(worker=worker)) or AUTO,
    }


def set_choice(
    conn: sqlite3.Connection, worker: str, *, model: str | None = None,
    effort: str | None = None,
) -> dict[str, str]:
    if worker not in SELECTABLE:
        raise ValueError(f"no selectable models for {worker}")
    if model is not None:
        model = model.strip() or AUTO
        if model != AUTO and model not in {entry["slug"] for entry in models_for(worker)}:
            raise ValueError(f"{worker} does not offer model '{model}'")
        settings.set_value(conn, MODEL_KEY.format(worker=worker), model)
    if effort is not None:
        effort = effort.strip() or AUTO
        current = model or choice(conn, worker)["model"]
        allowed = efforts_for(worker, current) if current != AUTO else (
            "low", "medium", "high", "xhigh", "max", "ultra"
        )
        if effort != AUTO and effort not in allowed:
            raise ValueError(f"{effort} is not a level {current} supports")
        settings.set_value(conn, EFFORT_KEY.format(worker=worker), effort)
    return choice(conn, worker)


def apply(
    conn: sqlite3.Connection, worker: str, *, model: str, effort: str | None,
    model_requested: bool, effort_requested: bool,
) -> tuple[str, str | None]:
    """Overlay the owner's defaults on a route, unless the task asked for one."""
    if worker not in SELECTABLE:
        return model, effort
    chosen = choice(conn, worker)
    if not model_requested and chosen["model"] != AUTO:
        model = chosen["model"]
    if not effort_requested and chosen["effort"] != AUTO:
        effort = chosen["effort"]
    if effort and effort not in efforts_for(worker, model):
        # A level the chosen model does not support would be rejected by the
        # CLI; fall back to its highest supported level instead of failing.
        supported = efforts_for(worker, model)
        effort = supported[-1] if supported else None
    return model, effort


def payload(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        worker: {
            "models": models_for(worker),
            "selected": choice(conn, worker),
        }
        for worker in SELECTABLE
    }
