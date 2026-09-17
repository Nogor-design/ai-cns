"""Provider quota ledger: may Cortex start another unattended run on a provider?

Subscription CLIs do not bill per call; they cut you off when a usage window is
full. So the question is not "how much will this cost" but "how full is each
window, and how much must stay free for the owner". Readings come from real
provider signals wherever they exist:

* Codex writes account-wide ``rate_limits`` (percent used, window length, reset
  time) into its session logs and JSON event stream.
* Claude emits ``rate_limit_event`` with five-hour and seven-day utilization in
  ``--output-format stream-json`` output.

Providers without such a signal (Grok, Gemini, Antigravity) are limited by a
count of unattended runs per rolling five hours. Local workers are governed by
``lanes`` instead. Any provider that refuses work is put on cooldown until its
reported reset time.

Readings are account-wide, so they include the owner's own interactive use --
which is exactly what the reserve protects.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import ids, settings

METERED: tuple[str, ...] = ("codex", "claude")
COUNTED: tuple[str, ...] = ("grok", "gemini", "agy")
LOCAL: tuple[str, ...] = ("ollama", "opencode")

RESERVE_KEY = "capacity.reserve_pct"
DEFAULT_RESERVE_PCT = 30.0
RUN_COST_KEY = "capacity.expected_run_pct"
DEFAULT_RUN_COST_PCT = 2.0
CALL_CAP_KEY = "capacity.counted_runs_per_5h"
DEFAULT_CALL_CAP = 10
COOLDOWN_FALLBACK = timedelta(minutes=30)

CODEX_SESSIONS_ENV = "CORTEX_CODEX_SESSIONS"
_TAIL_BYTES = 512_000
_MAX_SESSION_FILES = 8

_LIMIT_TEXT = re.compile(
    r"usage limit|rate[ _-]?limit(ed| reached| exceeded)|quota (exceeded|exhausted)"
    r"|too many requests|\b429\b",
    re.I,
)


@dataclass(frozen=True)
class Reading:
    provider: str
    window: str
    used_percent: float | None
    resets_at: str | None
    window_minutes: int | None = None
    limited: bool = False
    source: str = "run-output"
    source_ref: str | None = None
    observed_at: str = field(default_factory=ids.now)


@dataclass(frozen=True)
class Admission:
    provider: str
    allowed: bool
    reason: str
    kind: str                     # metered | counted | local | unknown
    ceiling_pct: float | None = None
    windows: tuple[dict[str, Any], ...] = ()


# ------------------------------------------------------------ settings ---
def reserve_pct(conn: sqlite3.Connection) -> float:
    return _clamp(settings.get_float(conn, RESERVE_KEY, DEFAULT_RESERVE_PCT), 0, 95)


def set_reserve_pct(conn: sqlite3.Connection, value: float) -> float:
    pct = float(value)
    if not 0 <= pct <= 95:
        raise ValueError("reserve must be between 0 and 95 percent")
    settings.set_value(conn, RESERVE_KEY, f"{pct:g}")
    return pct


def expected_run_pct(conn: sqlite3.Connection) -> float:
    return _clamp(settings.get_float(conn, RUN_COST_KEY, DEFAULT_RUN_COST_PCT), 0, 50)


def counted_cap(conn: sqlite3.Connection) -> int:
    return int(_clamp(settings.get_float(conn, CALL_CAP_KEY, DEFAULT_CALL_CAP), 0, 500))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ------------------------------------------------------------- parsing ---
def _epoch_iso(value: Any) -> str | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds > 10_000_000_000:  # milliseconds
        seconds /= 1000
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_name(minutes: Any) -> str:
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return "window"
    return {300: "five_hour", 10080: "seven_day"}.get(value, f"{value}m")


def _find_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = _find_key(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, key)
            if found is not None:
                return found
    return None


def parse_codex_rate_limits(
    limits: Any, *, source: str, source_ref: str | None = None,
    observed_at: str | None = None,
) -> list[Reading]:
    if not isinstance(limits, dict):
        return []
    limited = bool(limits.get("rate_limit_reached_type"))
    readings = []
    for slot in ("primary", "secondary"):
        data = limits.get(slot)
        if not isinstance(data, dict):
            continue
        used = data.get("used_percent")
        readings.append(Reading(
            provider="codex",
            window=_window_name(data.get("window_minutes")),
            window_minutes=data.get("window_minutes"),
            used_percent=float(used) if isinstance(used, (int, float)) else None,
            resets_at=_epoch_iso(data.get("resets_at")),
            limited=limited or (isinstance(used, (int, float)) and used >= 100),
            source=source,
            source_ref=source_ref,
            observed_at=observed_at or ids.now(),
        ))
    return readings


def parse_claude_rate_limit(
    info: Any, *, source: str, source_ref: str | None = None,
    observed_at: str | None = None,
) -> list[Reading]:
    if not isinstance(info, dict):
        return []
    rejected = str(info.get("status") or "").lower() not in {"", "allowed", "allowed_warning"}
    limited_window = info.get("rateLimitType")
    windows = info.get("unifiedWindows")
    readings = []
    if isinstance(windows, dict) and windows:
        for name, data in windows.items():
            if not isinstance(data, dict):
                continue
            utilization = data.get("utilization")
            readings.append(Reading(
                provider="claude",
                window=str(name),
                window_minutes={"five_hour": 300, "seven_day": 10080}.get(str(name)),
                used_percent=(
                    round(float(utilization) * 100, 2)
                    if isinstance(utilization, (int, float)) else None
                ),
                resets_at=_epoch_iso(data.get("resetsAt")),
                limited=rejected and (limited_window in {None, name}),
                source=source,
                source_ref=source_ref,
                observed_at=observed_at or ids.now(),
            ))
    elif rejected:
        readings.append(Reading(
            provider="claude", window=str(limited_window or "window"),
            used_percent=100.0, resets_at=_epoch_iso(info.get("resetsAt")),
            limited=True, source=source, source_ref=source_ref,
            observed_at=observed_at or ids.now(),
        ))
    return readings


def readings_from_output(
    provider: str, stdout: str, stderr: str = "", *,
    exit_code: int | None = None, source_ref: str | None = None,
) -> list[Reading]:
    """Quota readings and refusals found in one worker run's output."""
    readings: list[Reading] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        if "rate_limit" not in line and "usage limit" not in line.lower():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if provider == "codex":
            readings.extend(parse_codex_rate_limits(
                _find_key(event, "rate_limits"), source="run-output", source_ref=source_ref,
            ))
        elif provider == "claude" and event.get("type") == "rate_limit_event":
            readings.extend(parse_claude_rate_limit(
                event.get("rate_limit_info"), source="run-output", source_ref=source_ref,
            ))
    # Keep only the latest reading per window from this run.
    latest: dict[str, Reading] = {}
    for reading in readings:
        latest[reading.window] = reading
    readings = list(latest.values())

    refused = exit_code not in {None, 0} and _refusal_text(provider, stdout, stderr)
    if refused and not any(reading.limited for reading in readings):
        readings.append(Reading(
            provider=provider, window="refusal", used_percent=None,
            resets_at=(datetime.now(timezone.utc) + COOLDOWN_FALLBACK)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            limited=True, source="run-error", source_ref=source_ref,
        ))
    return readings


def _refusal_text(provider: str, stdout: str, stderr: str) -> bool:
    if provider == "claude":
        for line in reversed((stdout or "").splitlines()):
            if '"type":"result"' in line.replace(" ", ""):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    break
                return event.get("api_error_status") == 429 or bool(
                    _LIMIT_TEXT.search(str(event.get("result") or ""))
                )
    tail = f"{(stdout or '')[-4000:]}\n{(stderr or '')[-4000:]}"
    return bool(_LIMIT_TEXT.search(tail))


# ------------------------------------------------------ codex sessions ---
def codex_sessions_root() -> Path:
    override = os.environ.get(CODEX_SESSIONS_ENV)
    return Path(override) if override else Path.home() / ".codex" / "sessions"


def scan_codex_sessions(root: Path | None = None) -> list[Reading]:
    """Latest account-wide Codex readings from the newest session logs.

    Only the tail of a handful of recent files is read; the session directory
    can hold gigabytes.
    """
    base = root or codex_sessions_root()
    if not base.is_dir():
        return []
    candidates: list[tuple[float, Path]] = []
    for day_dir in _recent_day_dirs(base):
        for path in day_dir.glob("*.jsonl"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    candidates.sort(reverse=True)
    newest: dict[str, Reading] = {}
    for _, path in candidates[:_MAX_SESSION_FILES]:
        for reading in _last_codex_reading(path):
            known = newest.get(reading.window)
            if known is None or reading.observed_at > known.observed_at:
                newest[reading.window] = reading
    return list(newest.values())


def _recent_day_dirs(base: Path, days: int = 3) -> list[Path]:
    dirs = []
    for year in sorted(base.glob("[0-9][0-9][0-9][0-9]"), reverse=True)[:2]:
        for month in sorted(year.glob("[0-9][0-9]"), reverse=True)[:2]:
            dirs.extend(sorted(month.glob("[0-9][0-9]"), reverse=True))
    dirs.sort(key=lambda p: p.parts[-3:], reverse=True)
    return dirs[:days]


def _last_codex_reading(path: Path) -> list[Reading]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    for line in reversed(tail.splitlines()):
        if '"rate_limits"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        observed = _normalise_iso(event.get("timestamp")) if isinstance(event, dict) else None
        readings = parse_codex_rate_limits(
            _find_key(event, "rate_limits"), source="codex-session",
            source_ref=path.name, observed_at=observed,
        )
        if readings:
            return readings
    return []


def _normalise_iso(value: Any) -> str | None:
    parsed = _parse_iso(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else None


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------- storage ---
def record(conn: sqlite3.Connection, readings: Iterable[Reading]) -> int:
    """Store readings newer than what is already known; return how many."""
    stored = 0
    for reading in readings:
        latest = conn.execute(
            """SELECT observed_at, used_percent, resets_at, limited FROM quota_snapshots
               WHERE provider = ? AND window = ?
               ORDER BY observed_at DESC, id DESC LIMIT 1""",
            (reading.provider, reading.window),
        ).fetchone()
        if latest and (
            latest["observed_at"] > reading.observed_at
            or (
                latest["used_percent"] == reading.used_percent
                and latest["resets_at"] == reading.resets_at
                and bool(latest["limited"]) == reading.limited
            )
        ):
            continue
        conn.execute(
            """INSERT INTO quota_snapshots
               (provider, window, window_minutes, used_percent, resets_at, limited,
                source, source_ref, observed_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                reading.provider, reading.window, reading.window_minutes,
                reading.used_percent, reading.resets_at, int(reading.limited),
                reading.source, reading.source_ref, reading.observed_at,
            ),
        )
        stored += 1
    conn.commit()
    return stored


CLAUDE_PROBE_MIN_AGE = timedelta(minutes=15)
CLAUDE_PROBE_ARGV: tuple[str, ...] = (
    "claude", "-p", "Reply with the single word ok.",
    # No settings, hooks, MCP servers or tools: the smallest turn that still
    # uses the owner's login and returns the account's rate-limit windows.
    "--setting-sources", "", "--strict-mcp-config", "--tools", "",
    "--model", "haiku", "--max-turns", "1", "--no-session-persistence",
    "--output-format", "stream-json", "--verbose",
)


def refresh(conn: sqlite3.Connection, *, probe_stale: bool = False) -> int:
    """Pull local quota signals; optionally probe Claude when its reading is stale.

    Reading Codex session logs is free. A Claude probe is one tiny Haiku turn
    (a few thousand tokens), so it runs at most once per ``CLAUDE_PROBE_MIN_AGE``.
    """
    stored = record(conn, scan_codex_sessions())
    if probe_stale and claude_reading_age(conn) > CLAUDE_PROBE_MIN_AGE:
        stored += probe_claude(conn)
    return stored


def claude_reading_age(conn: sqlite3.Connection) -> timedelta:
    row = conn.execute(
        """SELECT MAX(observed_at) FROM quota_snapshots
           WHERE provider = 'claude' AND window != 'refusal'"""
    ).fetchone()
    observed = _parse_iso(row[0]) if row and row[0] else None
    if observed is None:
        return timedelta.max
    return datetime.now(timezone.utc) - observed


def probe_claude(conn: sqlite3.Connection, *, timeout: float = 90) -> int:
    import shutil
    import subprocess

    if shutil.which("claude") is None:
        return 0
    try:
        proc = subprocess.run(
            list(CLAUDE_PROBE_ARGV), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            cwd=str(Path.home()), stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return record(conn, [
        Reading(**{**reading.__dict__, "source": "claude-probe"})
        for reading in readings_from_output(
            "claude", proc.stdout, proc.stderr, exit_code=proc.returncode,
        )
    ])


def current_windows(
    conn: sqlite3.Connection, provider: str, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    moment = now or datetime.now(timezone.utc)
    rows = conn.execute(
        """SELECT q.* FROM quota_snapshots q
           JOIN (SELECT window, MAX(id) AS id FROM quota_snapshots
                 WHERE provider = ? GROUP BY window) latest ON latest.id = q.id
           ORDER BY q.window""",
        (provider,),
    ).fetchall()
    windows = []
    for row in rows:
        resets = _parse_iso(row["resets_at"])
        expired = resets is not None and resets <= moment
        if row["window"] == "refusal" and (expired or resets is None):
            continue
        windows.append({
            "window": row["window"],
            "window_minutes": row["window_minutes"],
            # Once a window has reset, the old percentage no longer applies.
            "used_percent": 0.0 if expired else row["used_percent"],
            "estimated": expired,
            "resets_at": None if expired else row["resets_at"],
            "limited": bool(row["limited"]) and not expired,
            "source": row["source"],
            "observed_at": row["observed_at"],
        })
    return windows


def _running_unattended(conn: sqlite3.Connection, provider: str) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM runs WHERE ended_at IS NULL
           AND started_by = 'scheduler' AND model LIKE ?
           AND started_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-3 hours')""",
        (f"{provider}:%",),
    ).fetchone()[0]


def _recent_unattended(conn: sqlite3.Connection, provider: str, since: datetime) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM runs WHERE started_by = 'scheduler'
           AND model LIKE ? AND started_at >= ?""",
        (f"{provider}:%", since.strftime("%Y-%m-%dT%H:%M:%SZ")),
    ).fetchone()[0]


def admit(
    conn: sqlite3.Connection, provider: str, *, now: datetime | None = None
) -> Admission:
    """Decide whether one more unattended run may start on ``provider``."""
    provider = provider.lower()
    moment = now or datetime.now(timezone.utc)
    if provider in LOCAL:
        return Admission(provider, True, "Local worker; governed by the local compute lane.", "local")

    windows = current_windows(conn, provider, now=moment)
    for window in windows:
        if window["limited"]:
            return Admission(
                provider, False,
                f"{provider} refused work; cooling down until {window['resets_at'] or 'its reset'}.",
                "metered" if provider in METERED else "counted", None, tuple(windows),
            )

    ceiling = 100.0 - reserve_pct(conn)
    if provider in METERED:
        measured = [w for w in windows if w["used_percent"] is not None and w["window"] != "refusal"]
        if not measured:
            if _running_unattended(conn, provider):
                return Admission(
                    provider, False,
                    f"No quota reading for {provider} yet; one unattended run at a time until one arrives.",
                    "metered", ceiling, tuple(windows),
                )
            return Admission(
                provider, True,
                f"No quota reading for {provider} yet; allowing one run to obtain one.",
                "metered", ceiling, tuple(windows),
            )
        cost = expected_run_pct(conn)
        for window in measured:
            if window["used_percent"] + cost > ceiling:
                return Admission(
                    provider, False,
                    f"{provider} {window['window'].replace('_', ' ')} window is "
                    f"{window['used_percent']:g}% used; unattended work stops at "
                    f"{ceiling:g}% to keep {100 - ceiling:g}% for you"
                    + (f" (resets {window['resets_at']})." if window["resets_at"] else "."),
                    "metered", ceiling, tuple(windows),
                )
        busiest = max(measured, key=lambda w: w["used_percent"])
        return Admission(
            provider, True,
            f"{provider} is at {busiest['used_percent']:g}% of its "
            f"{busiest['window'].replace('_', ' ')} window (limit {ceiling:g}%).",
            "metered", ceiling, tuple(windows),
        )

    if provider in COUNTED:
        cap = counted_cap(conn)
        used = _recent_unattended(conn, provider, moment - timedelta(hours=5))
        if used >= cap:
            return Admission(
                provider, False,
                f"{provider} has used {used} of {cap} unattended runs in the last five hours.",
                "counted", None, tuple(windows),
            )
        return Admission(
            provider, True,
            f"{provider} has used {used} of {cap} unattended runs in the last five hours.",
            "counted", None, tuple(windows),
        )
    return Admission(provider, False, f"{provider} has no quota policy.", "unknown")


def payload(conn: sqlite3.Connection, providers: Iterable[str]) -> dict[str, Any]:
    rows = []
    for provider in providers:
        admission = admit(conn, provider)
        rows.append({
            "provider": provider,
            "kind": admission.kind,
            "allowed": admission.allowed,
            "reason": admission.reason,
            "ceiling_pct": admission.ceiling_pct,
            "windows": list(admission.windows),
        })
    return {
        "reserve_pct": reserve_pct(conn),
        "expected_run_pct": expected_run_pct(conn),
        "counted_runs_per_5h": counted_cap(conn),
        "providers": rows,
    }
