"""Which projects Cortex may work on without the owner watching.

Two layers, deliberately unequal:

* **Protected projects** are decided here, in code. Trading systems and Apollo
  (a production application) are never worked on unattended. The dashboard and
  API can read this decision but cannot change it; lifting it takes a reviewed
  code change. The owner can still dispatch to these projects by hand.
* **Autonomy mode** is the owner's per-project setting for everything else:
  ``off`` (never unattended), ``read_only`` (unattended reviews/research only,
  the default) or ``integration`` (unattended writes that may merge into the
  project's integration branch once Phase 3 gates exist).

A global pause stops every unattended start at once.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from . import settings

MODES: tuple[str, ...] = ("off", "read_only", "integration")
DEFAULT_MODE = "read_only"

HUB_REGISTRY_ENV = "CORTEX_TRADING_HUB_REGISTRY"
DEFAULT_HUB_REGISTRY = Path(r"D:\trading-capability-hub\registry\capabilities.json")

# Trading roots known on 2026-09-17, kept even if the Hub registry is missing or
# unreadable so the exclusion never silently disappears.
TRADING_ROOTS: tuple[str, ...] = (
    r"D:\trading-capability-hub",
    r"D:\ta_foundation",
    r"D:\strategy-analysis",
    r"D:\MarketData",
    r"D:\nt-strategy-forge",
    r"D:\ninjatraderOptimizer",
    r"D:\NinjatraderAddons",
    r"D:\NinjaAccountManager",
    r"D:\ninjatrader-strategy-factory",
    r"D:\trader-dan-strategies",
    r"D:\DailyAnalysis",
    r"D:\daily-market-brief",
    r"D:\daily-card-generator",
    r"D:\stratum-framework",
)
TRADING_PROGRAMS: frozenset[str] = frozenset({"trading-systems"})
# Names are a backstop for trading repositories registered from new folders.
_TRADING_NAME = re.compile(
    r"trading|trader|ninja|\bnt8?\b|strateg|market-?data|futures|pantheon", re.I
)

APOLLO_ROOTS: tuple[str, ...] = (r"D:\apollo",)
_APOLLO_NAME = re.compile(r"apollo", re.I)

PAUSE_KEY = "autonomy.paused"


@dataclass(frozen=True)
class Protection:
    reason: str      # "trading" | "apollo"
    detail: str


def _field(project: Any, name: str) -> Any:
    if isinstance(project, sqlite3.Row):
        return project[name] if name in project.keys() else None
    return project.get(name)


def _norm(path: str) -> str:
    return str(PureWindowsPath(path)).rstrip("\\").lower()


def _under(path: str, root: str) -> bool:
    child, parent = _norm(path), _norm(root)
    return child == parent or child.startswith(parent + "\\")


def hub_trading_roots(registry: Path | None = None) -> tuple[str, ...]:
    """Owner paths listed in the Trading Capability Hub registry (read-only)."""
    target = registry or Path(os.environ.get(HUB_REGISTRY_ENV) or DEFAULT_HUB_REGISTRY)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    capabilities = data.get("capabilities", []) if isinstance(data, dict) else []
    return tuple(
        str(item["owner_path"])
        for item in capabilities
        if isinstance(item, dict) and item.get("owner_path")
    )


def protection(project: Any, *, registry: Path | None = None) -> Protection | None:
    """Return why a project is protected from unattended work, or None."""
    repo = str(_field(project, "repo_path") or "")
    name = " ".join(
        str(_field(project, key) or "") for key in ("id", "name")
    )
    program = str(_field(project, "program") or "").lower()

    if any(_under(repo, root) for root in APOLLO_ROOTS) or _APOLLO_NAME.search(name):
        return Protection("apollo", "Apollo is a production application; the owner monitors all work.")
    if program in TRADING_PROGRAMS:
        return Protection("trading", f"Program '{program}' is a trading program.")
    for root in (*TRADING_ROOTS, *hub_trading_roots(registry)):
        if repo and _under(repo, root):
            return Protection("trading", f"Repository is under trading root {root}.")
    if _TRADING_NAME.search(name):
        return Protection("trading", "Project name identifies a trading system.")
    return None


def mode(project: Any, *, registry: Path | None = None) -> str:
    """Effective autonomy mode; protected projects are always ``off``."""
    if protection(project, registry=registry):
        return "off"
    stored = str(_field(project, "autonomy_mode") or "").lower()
    return stored if stored in MODES else DEFAULT_MODE


def validate_mode_change(project: Any, requested: str) -> str:
    """Normalise an owner request, refusing to loosen a protected project."""
    value = str(requested or "").strip().lower()
    if value not in MODES:
        raise ValueError(f"autonomy mode must be one of: {', '.join(MODES)}")
    guard = protection(project)
    if guard and value != "off":
        raise ValueError(
            f"{_field(project, 'name')} is protected ({guard.reason}); "
            "unattended work cannot be enabled from Cortex."
        )
    return value


def is_paused(conn: sqlite3.Connection) -> bool:
    return settings.get_bool(conn, PAUSE_KEY, False)


def set_paused(conn: sqlite3.Connection, paused: bool) -> None:
    settings.set_value(conn, PAUSE_KEY, "1" if paused else "0")


def unattended_refusal(
    conn: sqlite3.Connection, project: Any, *, write: bool
) -> str | None:
    """Why an unattended start on this project is not allowed, or None."""
    if is_paused(conn):
        return "Autonomy is paused by the owner."
    guard = protection(project)
    if guard:
        return f"{_field(project, 'name')} is excluded from unattended work: {guard.detail}"
    current = mode(project)
    if current == "off":
        return f"{_field(project, 'name')} has autonomy turned off."
    if write and current != "integration":
        return f"{_field(project, 'name')} allows only read-only unattended work."
    return None


def summary(conn: sqlite3.Connection, projects: list[Any]) -> dict[str, Any]:
    rows = []
    for project in projects:
        guard = protection(project)
        rows.append({
            "project_id": _field(project, "id"),
            "name": _field(project, "name"),
            "mode": mode(project),
            "protected": guard.reason if guard else None,
            "protected_detail": guard.detail if guard else None,
        })
    return {"paused": is_paused(conn), "modes": list(MODES), "projects": rows}
