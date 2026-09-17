"""Refuse unattended tasks whose *content* is a trading procedure.

Project rules in ``autonomy`` exclude trading repositories. This adds the other
half: a task filed under an ordinary project that asks for a trading procedure
("backfill the market data", "sweep this strategy in NinjaTrader"). The Trading
Capability Hub owns those procedures and their gates, so its own playbook router
decides what counts; Cortex only reads it. A short built-in pattern list still
applies when the Hub is absent or its router fails, so a broken Hub can never
make the guard more permissive.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

HUB_ROOT_ENV = "CORTEX_TRADING_HUB_ROOT"
DEFAULT_HUB_ROOT = Path(r"D:\trading-capability-hub")
HUB_MIN_SCORE = 0.5

_BUILTIN = re.compile(
    r"ninja\s*trader|\bnt8?\b.*\b(strategy|analy[sz]er|optimi[sz]|backtest)|"
    r"strategy\s+analy[sz]er|marketdata|market[\s-]+data\s+(backfill|export|refresh)|"
    r"\b(live|paper|sim)\s+trad|\bplace\s+(an?\s+)?orders?\b|\bkill\s*switch\b|"
    r"\bflatten\b|\bpropguard\b|\bpantheon",
    re.IGNORECASE,
)

_lock = threading.Lock()
_cache: dict[str, Any] = {}


def hub_root() -> Path:
    return Path(os.environ.get(HUB_ROOT_ENV) or DEFAULT_HUB_ROOT)


def _hub_router() -> Any | None:
    """The Hub's PlaybookSet, loaded read-only by file path, cached by mtime."""
    root = hub_root()
    module_path = root / "src" / "trading_capability_hub" / "playbooks.py"
    registry = root / "registry" / "playbooks.json"
    try:
        stamp = (module_path.stat().st_mtime, registry.stat().st_mtime)
    except OSError:
        return None
    with _lock:
        if _cache.get("stamp") == stamp and _cache.get("root") == str(root):
            return _cache.get("router")
        router = None
        try:
            spec = importlib.util.spec_from_file_location("_cortex_hub_playbooks", module_path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                # dataclasses resolves annotations through sys.modules.
                sys.modules[spec.name] = module
                try:
                    spec.loader.exec_module(module)
                    router = module.load_playbooks(registry)
                finally:
                    sys.modules.pop(spec.name, None)
        except Exception:  # a broken Hub falls back to the built-in patterns
            router = None
        _cache.update(stamp=stamp, root=str(root), router=router)
        return router


def task_text(task: Any) -> str:
    parts = []
    for key in ("title", "brief", "acceptance", "next_action"):
        try:
            value = task[key]
        except (KeyError, IndexError):
            continue
        if value:
            parts.append(str(value))
    return "\n".join(parts)


def refusal(task: Any) -> str | None:
    """Why this task must not run unattended, or None."""
    text = task_text(task)
    if not text.strip():
        return None
    router = _hub_router()
    if router is not None:
        try:
            # The Hub accepts half of a phrase (score 0.5), which lets one shared
            # word match a two-word phrase: "Summarize open PRs" hits "open
            # ninjatrader". Requiring a majority keeps real asks (1.0) and leaves
            # loose paraphrases to the built-in patterns below.
            matches = [match for match in router.route(text) if match.score > HUB_MIN_SCORE]
        except Exception:
            matches = []
        if matches:
            ids = ", ".join(match.id for match in matches)
            return (
                f"Task matches Trading Capability Hub playbook(s) {ids}; trading "
                "procedures carry their own gates and never run unattended."
            )
    found = _BUILTIN.search(text)
    if found:
        return (
            f"Task mentions a trading operation ('{found.group(0).strip()}'); "
            "trading work never runs unattended."
        )
    return None
