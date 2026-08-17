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
SCHEMA_VERSION = 13

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
    remote_url      TEXT,
    github_owner    TEXT,
    github_repo     TEXT,
    github_project_owner TEXT,
    github_project_number INTEGER,
    github_project_id TEXT,
    codex_project_id TEXT,
    blueprint_path TEXT,
    blueprint_hash TEXT,
    blueprint_status TEXT NOT NULL DEFAULT 'missing',
    current_blueprint_revision_id TEXT,
    current_phase_id TEXT,
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
    parent_id      TEXT REFERENCES tasks(id),
    milestone      TEXT,
    start_at       TEXT,
    target_at      TEXT,
    completed_at   TEXT,
    progress       INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT,
    next_action    TEXT,
    github_issue_id TEXT,
    github_issue_number INTEGER,
    github_issue_url TEXT,
    github_project_item_id TEXT,
    codex_thread_id TEXT,
    pm_session_id  TEXT,
    phase_id       TEXT,
    exit_criterion_ref TEXT,
    sync_state     TEXT,
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

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id            TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type               TEXT NOT NULL DEFAULT 'blocks',
    created_at         TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS activity_events (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id),
    task_id     TEXT REFERENCES tasks(id),
    actor_type  TEXT NOT NULL DEFAULT 'agent',
    actor_name  TEXT,
    model       TEXT,
    action      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'cortex',
    source_ref  TEXT,
    session_id  TEXT,
    occurred_at TEXT NOT NULL,
    evidence_json TEXT
);

CREATE TABLE IF NOT EXISTS github_mirror_operations (
    operation_id     TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(id),
    project_id       TEXT NOT NULL REFERENCES projects(id),
    plan_fingerprint TEXT NOT NULL,
    github_project_id TEXT NOT NULL,
    github_issue_id  TEXT,
    github_project_item_id TEXT,
    status           TEXT NOT NULL, -- applying | interrupted | verified | refused | failed
    actor             TEXT NOT NULL,
    session_id        TEXT,
    actions_json      TEXT NOT NULL,
    completed_actions_json TEXT NOT NULL DEFAULT '[]',
    evidence_json     TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    completed_at      TEXT,
    UNIQUE(task_id, plan_fingerprint)
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
    blueprint_revision_id TEXT,
    phase_id           TEXT,
    exit_criterion_ref TEXT,
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

-- Project deletion is intentionally auditable even after all project-bound
-- rows are gone. This tombstone has no foreign key back to projects so the
-- portfolio timeline can still answer who removed what and when.
CREATE TABLE IF NOT EXISTS project_removals (
    removal_id         TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL,
    project_name       TEXT NOT NULL,
    repo_path          TEXT NOT NULL,
    actor_type         TEXT NOT NULL DEFAULT 'human',
    actor_name         TEXT,
    source             TEXT NOT NULL DEFAULT 'dashboard',
    removed_at         TEXT NOT NULL,
    deleted_counts_json TEXT NOT NULL,
    evidence_json      TEXT
);

CREATE TABLE IF NOT EXISTS project_blueprint_drafts (
    project_id      TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    stage           TEXT NOT NULL DEFAULT 'discovery',
    answers_json    TEXT NOT NULL DEFAULT '{}',
    discovery_hash TEXT NOT NULL,
    discovery_json TEXT NOT NULL DEFAULT '{}',
    preview_json    TEXT,
    preview_fingerprint TEXT,
    previewed_at    TEXT,
    planning_task_id TEXT REFERENCES tasks(id),
    pm_session_id   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_blueprint_revisions (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ordinal           INTEGER NOT NULL,
    path              TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    markdown_content  TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'approved',
    pm_session_id     TEXT,
    approving_actor   TEXT NOT NULL,
    approved_at       TEXT NOT NULL,
    source_git_commit TEXT,
    plan_basis_json   TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    UNIQUE(project_id, ordinal)
);

CREATE TABLE IF NOT EXISTS project_phases (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    blueprint_revision_id TEXT NOT NULL REFERENCES project_blueprint_revisions(id) ON DELETE CASCADE,
    ordinal             INTEGER NOT NULL,
    name                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'planned',
    outcome             TEXT NOT NULL,
    non_goals           TEXT,
    entry_criteria_json TEXT NOT NULL DEFAULT '[]',
    exit_criteria_json  TEXT NOT NULL DEFAULT '[]',
    start_at            TEXT,
    target_at           TEXT,
    completed_at        TEXT,
    pm_session_id       TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(blueprint_revision_id, ordinal)
);

CREATE TABLE IF NOT EXISTS phase_dependencies (
    phase_id            TEXT NOT NULL REFERENCES project_phases(id) ON DELETE CASCADE,
    depends_on_phase_id TEXT NOT NULL REFERENCES project_phases(id) ON DELETE CASCADE,
    type                TEXT NOT NULL DEFAULT 'blocks',
    created_at          TEXT NOT NULL,
    PRIMARY KEY (phase_id, depends_on_phase_id)
);

CREATE TABLE IF NOT EXISTS phase_criterion_evidence (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    phase_id              TEXT NOT NULL REFERENCES project_phases(id) ON DELETE CASCADE,
    blueprint_revision_id TEXT NOT NULL REFERENCES project_blueprint_revisions(id) ON DELETE CASCADE,
    exit_criterion_ref    TEXT NOT NULL,
    criterion_text        TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'accepted',
    evidence_json         TEXT NOT NULL,
    preview_fingerprint   TEXT NOT NULL,
    approving_actor       TEXT NOT NULL,
    accepted_at           TEXT NOT NULL,
    UNIQUE(phase_id, exit_criterion_ref, preview_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project_id);
CREATE INDEX IF NOT EXISTS idx_dependencies_task ON task_dependencies(task_id);
CREATE INDEX IF NOT EXISTS idx_dependencies_upstream ON task_dependencies(depends_on_task_id);
CREATE INDEX IF NOT EXISTS idx_activity_project_time ON activity_events(project_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_task_time ON activity_events(task_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_github_mirror_task_time
    ON github_mirror_operations(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_github_mirror_status
    ON github_mirror_operations(status);
CREATE INDEX IF NOT EXISTS idx_suggestions_project ON suggestions(project_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);
CREATE INDEX IF NOT EXISTS idx_project_removals_time
    ON project_removals(removed_at DESC);
CREATE INDEX IF NOT EXISTS idx_blueprint_revisions_project
    ON project_blueprint_revisions(project_id, ordinal DESC);
CREATE INDEX IF NOT EXISTS idx_project_phases_project
    ON project_phases(project_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_phase_dependencies_upstream
    ON phase_dependencies(depends_on_phase_id);
CREATE INDEX IF NOT EXISTS idx_phase_criterion_evidence
    ON phase_criterion_evidence(phase_id, exit_criterion_ref, accepted_at DESC);

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
        "remote_url": "TEXT",
        "github_owner": "TEXT",
        "github_repo": "TEXT",
        "github_project_owner": "TEXT",
        "github_project_number": "INTEGER",
        "github_project_id": "TEXT",
        "codex_project_id": "TEXT",
        "blueprint_path": "TEXT",
        "blueprint_hash": "TEXT",
        "blueprint_status": "TEXT NOT NULL DEFAULT 'missing'",
        "current_blueprint_revision_id": "TEXT",
        "current_phase_id": "TEXT",
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
        "parent_id": "TEXT REFERENCES tasks(id)",
        "milestone": "TEXT",
        "start_at": "TEXT",
        "target_at": "TEXT",
        "completed_at": "TEXT",
        "progress": "INTEGER NOT NULL DEFAULT 0",
        "blocked_reason": "TEXT",
        "next_action": "TEXT",
        "github_issue_id": "TEXT",
        "github_issue_number": "INTEGER",
        "github_issue_url": "TEXT",
        "github_project_item_id": "TEXT",
        "codex_thread_id": "TEXT",
        "pm_session_id": "TEXT",
        "phase_id": "TEXT",
        "exit_criterion_ref": "TEXT",
        "sync_state": "TEXT",
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
        "blueprint_revision_id": "TEXT",
        "phase_id": "TEXT",
        "exit_criterion_ref": "TEXT",
    },
    "activity_events": {
        "session_id": "TEXT",
    },
    "project_blueprint_drafts": {
        "planning_task_id": "TEXT REFERENCES tasks(id)",
        "pm_session_id": "TEXT",
        "preview_json": "TEXT",
        "preview_fingerprint": "TEXT",
        "previewed_at": "TEXT",
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


def _apply_identity_constraints(conn: sqlite3.Connection) -> None:
    """Keep one Cortex task mapped to one stable GitHub identity.

    These indexes run after additive migrations because older task tables do not
    yet contain the GitHub columns when the main schema script is evaluated.
    """
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_github_issue_id
           ON tasks(github_issue_id) WHERE github_issue_id IS NOT NULL"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_github_project_item_id
           ON tasks(github_project_item_id)
           WHERE github_project_item_id IS NOT NULL"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_suggestions_phase
           ON suggestions(phase_id, exit_criterion_ref)"""
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
    _apply_identity_constraints(conn)
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
