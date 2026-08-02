"""SQLite schema and connection management for Cortex-Lite.

Single file, zero config (spec section 4). The canonical project state lives as
a git-tracked markdown file inside each repo; this database indexes and links to
it rather than owning it. Model performance is a VIEW over `runs`, never a stored
table, so it can never go stale.

Several agents and the dashboard poll this database concurrently, so connections
are configured for concurrent use: WAL journaling lets readers proceed while a
worker writes, and a generous busy timeout absorbs the short write bursts that
dispatch produces. Schema setup runs once per database rather than on every
connect, because the dashboard opens a connection per HTTP request.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from . import config

# Bump when SCHEMA or _ADDITIVE_COLUMNS change so existing databases re-run setup.
SCHEMA_VERSION = 3

# Long enough to outlast the write bursts at the start and end of a dispatch,
# short enough that a genuine deadlock still surfaces as an error.
BUSY_TIMEOUT_MS = 30_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    repo_path     TEXT NOT NULL,
    stack         TEXT,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | paused | archived
    program       TEXT NOT NULL DEFAULT 'general',
    priority      INTEGER NOT NULL DEFAULT 3,       -- 1 (highest) .. 5 (lowest)
    privacy       TEXT NOT NULL DEFAULT 'internal', -- public | internal | restricted
    state_mode    TEXT NOT NULL DEFAULT 'tracked',  -- tracked | deferred
    current_goal  TEXT,
    test_command  TEXT,
    -- JSON array of worker names permitted to see this repository. NULL means
    -- "not configured yet"; the policy layer supplies a privacy-based default.
    allowed_workers TEXT,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id),
    title          TEXT NOT NULL,
    type           TEXT NOT NULL DEFAULT 'other',   -- code|review|research|docs|data|planning|other
    status         TEXT NOT NULL DEFAULT 'open',    -- open|in_progress|done|abandoned
    brief          TEXT,
    execution_mode TEXT,                            -- agentic_cli|api|manual
    model          TEXT,
    risk           TEXT NOT NULL DEFAULT 'auto',    -- auto | low | medium | high
    complexity     INTEGER,                         -- optional 0..10 override
    acceptance     TEXT,
    allowed_paths  TEXT,                            -- JSON array or newline-delimited text
    budget         TEXT,                            -- local | small | medium | large
    requested_model TEXT,                           -- optional explicit CLI model
    effort          TEXT,                           -- low | medium | high | xhigh
    priority       INTEGER NOT NULL DEFAULT 3,       -- 1 (highest) .. 5 (lowest)
    assignee       TEXT,                            -- explicit worker or human owner
    due_at         TEXT,                            -- optional ISO date/datetime
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES tasks(id),
    project_id     TEXT NOT NULL REFERENCES projects(id),
    model          TEXT,
    execution_mode TEXT,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    git_before     TEXT,
    git_after      TEXT,
    files_changed  TEXT,        -- JSON array
    diff_size      INTEGER,
    tests_passed   INTEGER,     -- 1 | 0 | NULL
    outcome        TEXT,        -- survived | reverted | unknown
    response       TEXT,
    captured_via   TEXT,        -- git | api | manual
    human_note     TEXT,
    workspace_path TEXT,
    command_json   TEXT,
    usage_json     TEXT,
    effort         TEXT,
    exit_code      INTEGER
);

CREATE TABLE IF NOT EXISTS decisions (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id),
    ts          TEXT NOT NULL,
    decision    TEXT NOT NULL,
    rationale   TEXT,
    source      TEXT
);

CREATE TABLE IF NOT EXISTS suggestions (
    id                 TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL REFERENCES projects(id),
    title              TEXT NOT NULL,
    type               TEXT NOT NULL DEFAULT 'other',
    status             TEXT NOT NULL DEFAULT 'proposed', -- proposed|converted|dismissed
    why                TEXT,
    brief              TEXT,
    risk               TEXT NOT NULL DEFAULT 'auto',
    complexity         INTEGER,
    acceptance         TEXT,
    allowed_paths      TEXT,
    budget             TEXT,
    effort             TEXT,
    priority           INTEGER NOT NULL DEFAULT 3,
    recommended_worker TEXT,
    recommended_model  TEXT,
    action             TEXT,
    reviewer           TEXT,
    requires_approval  INTEGER NOT NULL DEFAULT 0,
    source_worker      TEXT NOT NULL DEFAULT 'deterministic',
    source_model       TEXT,
    task_id            TEXT REFERENCES tasks(id),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- Background work started by the dashboard. Persisted rather than held in
-- memory so a restart can report what was interrupted instead of silently
-- losing runs that the UI still shows as active.
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,          -- plan | dispatch | git
    status       TEXT NOT NULL,          -- running | done | failed | interrupted
    task_id      TEXT,
    project_id   TEXT,
    run_id       TEXT,
    label        TEXT,
    result_json  TEXT,
    error        TEXT,
    pid          INTEGER,                -- owning dashboard process
    created_at   TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);

CREATE TABLE IF NOT EXISTS project_git_checks (
    project_id      TEXT PRIMARY KEY REFERENCES projects(id),
    is_git          INTEGER NOT NULL DEFAULT 0,
    branch          TEXT,
    modified        INTEGER NOT NULL DEFAULT 0,
    untracked       INTEGER NOT NULL DEFAULT 0,
    ahead           INTEGER,
    behind          INTEGER,
    last_commit_date TEXT,
    last_commit_sha TEXT,
    last_commit_subject TEXT,
    fetched         INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    checked_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_project ON suggestions(project_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);

-- Model history for decision support: computed, never stored (spec section 4).
-- Recreate it on connect so additive outcome semantics reach older databases.
DROP VIEW IF EXISTS model_task_history;
CREATE VIEW model_task_history AS
SELECT runs.project_id            AS project_id,
       tasks.type                 AS task_type,
       runs.model                 AS model,
       COUNT(*)                   AS attempts,
       SUM(runs.tests_passed = 1) AS tests_passed,
       SUM(runs.outcome IN ('survived', 'accepted')) AS survived,
       AVG(runs.diff_size)        AS avg_diff_size,
       MAX(runs.started_at)       AS last_used
FROM runs JOIN tasks ON runs.task_id = tasks.id
GROUP BY runs.project_id, tasks.type, runs.model;
"""


# Existing Cortex databases predate the portfolio/dispatcher fields. SQLite's
# CREATE TABLE IF NOT EXISTS does not add columns, so keep a tiny additive
# migration map here. All fields are nullable or have safe defaults.
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "projects": {
        "program": "TEXT NOT NULL DEFAULT 'general'",
        "priority": "INTEGER NOT NULL DEFAULT 3",
        "privacy": "TEXT NOT NULL DEFAULT 'internal'",
        "state_mode": "TEXT NOT NULL DEFAULT 'tracked'",
        "allowed_workers": "TEXT",
    },
    "tasks": {
        "risk": "TEXT NOT NULL DEFAULT 'auto'",
        "complexity": "INTEGER",
        "acceptance": "TEXT",
        "allowed_paths": "TEXT",
        "budget": "TEXT",
        "priority": "INTEGER NOT NULL DEFAULT 3",
        "assignee": "TEXT",
        "due_at": "TEXT",
        "requested_model": "TEXT",
        "effort": "TEXT",
    },
    "runs": {
        "workspace_path": "TEXT",
        "command_json": "TEXT",
        "usage_json": "TEXT",
        "exit_code": "INTEGER",
        "effort": "TEXT",
    },
    "suggestions": {
        "effort": "TEXT",
    },
}


def _apply_additive_migrations(conn: sqlite3.Connection) -> None:
    """Add portfolio fields to databases created by Cortex-Lite 1.0."""
    for table, columns in _ADDITIVE_COLUMNS.items():
        existing = {
            row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}'
                )


class Connection(sqlite3.Connection):
    """A connection whose ``with`` block also closes it.

    ``sqlite3``'s own context manager commits or rolls back but deliberately
    leaves the connection open. Every dashboard request opens one, so relying on
    the default leaks a handle per poll; closing on exit keeps request-scoped
    usage honest.
    """

    def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


_initialised: set[str] = set()
_init_lock = threading.Lock()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create or upgrade the schema, at most once per database per process."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    conn.executescript(SCHEMA)
    _apply_additive_migrations(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def connect(path: str | Path | None = None) -> Connection:
    """Open a connection configured for concurrent dashboard and worker use."""
    target = Path(path) if path is not None else config.db_path()
    key = str(target.resolve() if target.parent.exists() else target)
    conn = sqlite3.connect(str(target), timeout=BUSY_TIMEOUT_MS / 1000, factory=Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    # WAL lets the dashboard keep reading while a dispatch writes. It is a
    # persistent property of the file, so setting it once would do; it is cheap
    # to assert and keeps freshly created databases correct.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    if key not in _initialised:
        with _init_lock:
            if key not in _initialised:
                _ensure_schema(conn)
                _initialised.add(key)
    return conn
