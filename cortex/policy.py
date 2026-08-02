"""Per-project worker policy: which CLIs may see which repository.

This replaces the old implicit rule where ``privacy == 'restricted'`` silently
forced every task onto Ollama while the router simultaneously recommended a
cloud CLI. The two disagreed, so restricted work could be routed but never
dispatched.

The policy here is explicit and answers one question: *which workers is this
repository willing to expose itself to?* Everything else follows from it. The
router picks the best worker it is allowed to pick, and dispatch refuses only
when a worker is genuinely off the list.

Two separate guarantees, kept separate on purpose:

* This module controls **who may read the repository** (a privacy question).
* ``allow_write`` and isolated worktrees control **whether files change**
  (a blast-radius question).

Conflating them is what produced tasks that no button could start.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

# Every worker Cortex knows how to drive.
ALL_WORKERS: tuple[str, ...] = (
    "codex", "claude", "gemini", "grok", "ollama", "perplexity",
)

# Workers that never send repository content off this machine.
LOCAL_WORKERS: frozenset[str] = frozenset({"ollama"})

# Applied only when a project has no explicit allowlist yet. Restricted repos
# stay local until their owner opts in, so an unconfigured project can never
# leak by default.
DEFAULT_BY_PRIVACY: dict[str, tuple[str, ...]] = {
    "public": ALL_WORKERS,
    "internal": ALL_WORKERS,
    "restricted": ("ollama",),
}

# Order used when the router's first choice is not permitted. Earlier entries
# are preferred. See choose_worker() for how this is applied.
PREFERENCE_ORDER: tuple[str, ...] = (
    "codex", "claude", "gemini", "grok", "ollama",
)


def _field(project: Any, name: str) -> Any:
    """Read a column from either a sqlite3.Row or a plain dict."""
    if isinstance(project, sqlite3.Row):
        return project[name] if name in project.keys() else None
    return project.get(name)


def allowed_workers(project: Any) -> tuple[str, ...]:
    """Return the workers permitted to read this project's repository."""
    raw = _field(project, "allowed_workers")
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            parsed = [part.strip() for part in str(raw).replace(",", "\n").splitlines()]
        names = tuple(
            name for name in (str(item).strip().lower() for item in parsed or [])
            if name in ALL_WORKERS
        )
        if names:
            return names
    privacy = str(_field(project, "privacy") or "internal")
    return DEFAULT_BY_PRIVACY.get(privacy, ALL_WORKERS)


def is_configured(project: Any) -> bool:
    """True when the owner has set an explicit allowlist for this project."""
    return bool(_field(project, "allowed_workers"))


def is_allowed(project: Any, worker: str) -> bool:
    return str(worker or "").lower() in allowed_workers(project)


def encode(workers: Iterable[str]) -> str:
    """Normalise an allowlist for storage, preserving ALL_WORKERS order."""
    chosen = {str(name).strip().lower() for name in workers}
    unknown = chosen - set(ALL_WORKERS)
    if unknown:
        raise ValueError(f"unknown worker(s): {', '.join(sorted(unknown))}")
    ordered = [name for name in ALL_WORKERS if name in chosen]
    if not ordered:
        raise ValueError("a project must allow at least one worker")
    return json.dumps(ordered)


def choose_worker(project: Any, preferred: str) -> str | None:
    """Pick the worker to actually use, given the project's allowlist.

    ``preferred`` is the router's first choice on task characteristics alone.
    When it is permitted, it wins. When it is not, this decides what happens
    instead: substitute the best permitted alternative, or give up and let the
    caller report the task as blocked.

    Returns the worker to run, or None when nothing permitted can do the job.
    """
    permitted = allowed_workers(project)
    preferred = str(preferred or "").lower()
    if preferred in permitted:
        return preferred

    # Perplexity has no local CLI, so it can never be an automatic substitute
    # even when a project permits it; it stays a deliberate manual choice.
    candidates = [
        name for name in PREFERENCE_ORDER
        if name in permitted and name != "perplexity"
    ]
    return candidates[0] if candidates else None


def explain(project: Any, preferred: str, chosen: str | None) -> str | None:
    """One human-readable sentence about a substitution, for the UI and logs."""
    if chosen == str(preferred or "").lower():
        return None
    name = _field(project, "name") or _field(project, "id")
    if chosen is None:
        return (
            f"{name} does not permit any worker that can run this task; "
            f"add {preferred} to its allowlist or pick a different task."
        )
    return (
        f"{preferred} is not on {name}'s allowlist; using {chosen} instead."
    )
