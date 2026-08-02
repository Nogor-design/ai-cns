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
