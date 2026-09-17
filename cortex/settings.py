"""Small owner-adjustable key/value settings stored in the portfolio database."""

from __future__ import annotations

import sqlite3

from . import ids


def get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_bool(conn: sqlite3.Connection, key: str, default: bool) -> bool:
    value = get(conn, key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_float(conn: sqlite3.Connection, key: str, default: float) -> float:
    value = get(conn, key)
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def set_value(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                          updated_at = excluded.updated_at""",
        (key, value, ids.now()),
    )
    conn.commit()
