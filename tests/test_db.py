"""SQLite connection and concurrency contracts."""

from __future__ import annotations

from cortex import db


def test_connections_use_wal_and_can_read_during_a_write_transaction(isolated_db):
    writer = db.connect(isolated_db)
    reader = db.connect(isolated_db)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            """INSERT INTO jobs (id, kind, status, created_at)
               VALUES ('uncommitted', 'plan', 'running', '2026-08-02T00:00:00Z')"""
        )

        # WAL readers see the last committed snapshot instead of failing with
        # "database is locked" while a worker is writing.
        visible = reader.execute(
            "SELECT COUNT(*) FROM jobs WHERE id = 'uncommitted'"
        ).fetchone()[0]
        assert visible == 0
    finally:
        writer.rollback()
        reader.close()
        writer.close()


def test_connection_context_manager_closes_request_scoped_handle(isolated_db):
    with db.connect(isolated_db) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1

    try:
        conn.execute("SELECT 1")
    except Exception as exc:
        assert "closed" in str(exc).lower()
    else:
        raise AssertionError("connection remained open after its with block")


def test_v5_database_adds_unique_github_identity_indexes(isolated_db):
    with db.connect(isolated_db) as conn:
        conn.execute("DROP INDEX idx_tasks_github_issue_id")
        conn.execute("DROP INDEX idx_tasks_github_project_item_id")
        conn.execute("PRAGMA user_version = 5")

    key = str(isolated_db.resolve())
    db._initialised.discard(key)
    with db.connect(isolated_db) as conn:
        indexes = {
            row["name"]: row["unique"]
            for row in conn.execute("PRAGMA index_list(tasks)").fetchall()
        }
        assert indexes["idx_tasks_github_issue_id"] == 1
        assert indexes["idx_tasks_github_project_item_id"] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_v6_database_adds_github_mirror_configuration_and_operations(isolated_db):
    with db.connect(isolated_db) as conn:
        conn.execute("DROP TABLE github_mirror_operations")
        conn.execute("PRAGMA user_version = 6")

    key = str(isolated_db.resolve())
    db._initialised.discard(key)
    with db.connect(isolated_db) as conn:
        project_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(projects)")
        }
        assert {
            "github_project_owner", "github_project_number", "github_project_id"
        } <= project_columns
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='github_mirror_operations'"
        ).fetchone()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_v7_database_adds_project_removal_audit_table(isolated_db):
    with db.connect(isolated_db) as conn:
        conn.execute("DROP TABLE project_removals")
        conn.execute("PRAGMA user_version = 7")

    key = str(isolated_db.resolve())
    db._initialised.discard(key)
    with db.connect(isolated_db) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_removals'"
        ).fetchone()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_v8_database_adds_blueprint_revision_and_phase_schema(isolated_db):
    with db.connect(isolated_db) as conn:
        for table in (
            "phase_criterion_evidence", "phase_dependencies", "project_phases",
            "project_blueprint_revisions", "project_blueprint_drafts",
        ):
            conn.execute(f'DROP TABLE "{table}"')
        conn.execute("PRAGMA user_version = 8")

    key = str(isolated_db.resolve())
    db._initialised.discard(key)
    with db.connect(isolated_db) as conn:
        project_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(projects)")
        }
        task_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)")
        }
        suggestion_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(suggestions)")
        }
        assert {
            "blueprint_path", "blueprint_hash", "blueprint_status",
            "current_blueprint_revision_id", "current_phase_id",
        } <= project_columns
        assert {"phase_id", "exit_criterion_ref"} <= task_columns
        assert {
            "blueprint_revision_id", "phase_id", "exit_criterion_ref",
        } <= suggestion_columns
        for table in (
            "project_blueprint_drafts", "project_blueprint_revisions",
            "project_phases", "phase_dependencies", "phase_criterion_evidence",
        ):
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_v9_database_adds_blueprint_draft_pm_attribution(isolated_db):
    with db.connect(isolated_db) as conn:
        conn.execute("ALTER TABLE project_blueprint_drafts RENAME TO old_blueprint_drafts")
        conn.execute(
            """CREATE TABLE project_blueprint_drafts (
               project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
               stage TEXT NOT NULL DEFAULT 'discovery',
               answers_json TEXT NOT NULL DEFAULT '{}',
               discovery_hash TEXT NOT NULL,
               discovery_json TEXT NOT NULL DEFAULT '{}',
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL)"""
        )
        conn.execute("DROP TABLE old_blueprint_drafts")
        conn.execute("PRAGMA user_version = 9")

    key = str(isolated_db.resolve())
    db._initialised.discard(key)
    with db.connect(isolated_db) as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(project_blueprint_drafts)")
        }
        assert {"planning_task_id", "pm_session_id"} <= columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_v10_database_adds_phase_links_to_existing_suggestions(isolated_db):
    with db.connect(isolated_db) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_suggestions_phase")
        conn.execute("ALTER TABLE suggestions RENAME TO old_suggestions")
        conn.execute(
            """CREATE TABLE suggestions (
               id TEXT PRIMARY KEY,
               project_id TEXT NOT NULL REFERENCES projects(id),
               title TEXT NOT NULL,
               type TEXT NOT NULL DEFAULT 'other',
               status TEXT NOT NULL DEFAULT 'proposed',
               why TEXT, brief TEXT, risk TEXT NOT NULL DEFAULT 'auto',
               complexity INTEGER, acceptance TEXT, allowed_paths TEXT,
               budget TEXT, effort TEXT, priority INTEGER NOT NULL DEFAULT 3,
               recommended_worker TEXT, recommended_model TEXT, action TEXT,
               reviewer TEXT, requires_approval INTEGER NOT NULL DEFAULT 0,
               source_worker TEXT NOT NULL DEFAULT 'deterministic',
               source_model TEXT, task_id TEXT REFERENCES tasks(id),
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
        )
        conn.execute("DROP TABLE old_suggestions")
        conn.execute("PRAGMA user_version = 10")

    db._initialised.discard(str(isolated_db.resolve()))
    with db.connect(isolated_db) as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(suggestions)")
        }
        assert {
            "blueprint_revision_id", "phase_id", "exit_criterion_ref",
        } <= columns
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_suggestions_phase'"
        ).fetchone()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_v11_database_adds_phase_criterion_evidence(isolated_db):
    with db.connect(isolated_db) as conn:
        conn.execute("DROP TABLE phase_criterion_evidence")
        conn.execute("PRAGMA user_version = 11")

    db._initialised.discard(str(isolated_db.resolve()))
    with db.connect(isolated_db) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase_criterion_evidence'"
        ).fetchone()
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(phase_criterion_evidence)")
        }
        assert {
            "phase_id", "exit_criterion_ref", "evidence_json",
            "preview_fingerprint", "approving_actor", "accepted_at",
        } <= columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_v12_database_adds_restart_safe_blueprint_preview_columns(isolated_db):
    with db.connect(isolated_db) as conn:
        conn.execute("ALTER TABLE project_blueprint_drafts RENAME TO old_blueprint_drafts")
        conn.execute(
            """CREATE TABLE project_blueprint_drafts (
               project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
               stage TEXT NOT NULL DEFAULT 'discovery',
               answers_json TEXT NOT NULL DEFAULT '{}',
               discovery_hash TEXT NOT NULL,
               discovery_json TEXT NOT NULL DEFAULT '{}',
               planning_task_id TEXT REFERENCES tasks(id),
               pm_session_id TEXT,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL)"""
        )
        conn.execute("DROP TABLE old_blueprint_drafts")
        conn.execute("PRAGMA user_version = 12")

    db._initialised.discard(str(isolated_db.resolve()))
    with db.connect(isolated_db) as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(project_blueprint_drafts)")
        }
        assert {"preview_json", "preview_fingerprint", "previewed_at"} <= columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
