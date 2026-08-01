"""Schema, CRUD, and the model_task_history VIEW."""

from __future__ import annotations

import pytest

from cortex import ids, store


def test_slugify():
    assert ids.slugify("Demo Project") == "demo-project"
    assert ids.slugify("  Weird!! Name ") == "weird-name"
    assert ids.slugify("") == "project"


def test_project_crud_and_lookup_by_name(conn, git_repo):
    pid = store.create_project(conn, name="My Repo", repo_path=str(git_repo))
    assert pid == "my-repo"
    by_id = store.get_project(conn, "my-repo")
    by_name = store.get_project(conn, "My Repo")
    assert by_id["id"] == by_name["id"] == "my-repo"
    with pytest.raises(store.NotFound):
        store.get_project(conn, "nope")


def test_task_type_coerced(conn, project):
    tid = store.create_task(conn, project_id=project["id"], title="t", type="bogus")
    assert store.get_task(conn, tid)["type"] == "other"
    tid2 = store.create_task(conn, project_id=project["id"], title="t2", type="code")
    assert store.get_task(conn, tid2)["type"] == "code"


def test_runs_json_files_changed(conn, project):
    tid = store.create_task(conn, project_id=project["id"], title="t", type="code")
    rid = store.create_run(
        conn, task_id=tid, project_id=project["id"], model="m", execution_mode="api"
    )
    store.update_run(conn, rid, files_changed=["a.py", "b.py"], diff_size=10)
    row = store.get_run(conn, rid)
    assert row["files_changed"] == '["a.py", "b.py"]'
    assert row["diff_size"] == 10


def test_model_task_history_view(conn, project):
    tid = store.create_task(conn, project_id=project["id"], title="t", type="code")
    for passed, outcome in ((1, "survived"), (1, "reverted"), (0, "accepted")):
        rid = store.create_run(
            conn, task_id=tid, project_id=project["id"],
            model="codex", execution_mode="agentic_cli",
        )
        store.update_run(conn, rid, tests_passed=passed, outcome=outcome, diff_size=20)
    rows = store.model_task_history(conn, project["id"], task_type="code")
    assert len(rows) == 1
    r = rows[0]
    assert r["model"] == "codex"
    assert r["attempts"] == 3
    assert r["tests_passed"] == 2
    assert r["survived"] == 2
    assert r["avg_diff_size"] == 20


def test_decisions(conn, project):
    store.create_decision(
        conn, project_id=project["id"], decision="use sqlite", rationale="boring", source="manual"
    )
    rows = store.recent_decisions(conn, project["id"])
    assert rows[0]["decision"] == "use sqlite"
