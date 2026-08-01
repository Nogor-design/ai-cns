"""Portfolio health, routing, workers, worktrees, and dispatch safety."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cortex import config, dispatcher, evidence, health, routing, store, workers, worktrees


def test_additive_migration_upgrades_old_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projects (
          id TEXT PRIMARY KEY, name TEXT, repo_path TEXT, stack TEXT,
          status TEXT, current_goal TEXT, test_command TEXT, updated_at TEXT
        );
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, project_id TEXT, title TEXT, type TEXT,
          status TEXT, brief TEXT, execution_mode TEXT, model TEXT,
          created_at TEXT, updated_at TEXT
        );
        CREATE TABLE runs (
          id TEXT PRIMARY KEY, task_id TEXT, project_id TEXT, model TEXT,
          execution_mode TEXT, started_at TEXT, ended_at TEXT, git_before TEXT,
          git_after TEXT, files_changed TEXT, diff_size INTEGER,
          tests_passed INTEGER, outcome TEXT, response TEXT,
          captured_via TEXT, human_note TEXT
        );
        CREATE TABLE decisions (
          id TEXT PRIMARY KEY, project_id TEXT, ts TEXT, decision TEXT,
          rationale TEXT, source TEXT
        );
        """
    )
    conn.close()

    from cortex import db

    upgraded = db.connect(path)
    project_columns = {row[1] for row in upgraded.execute("pragma table_info(projects)")}
    task_columns = {row[1] for row in upgraded.execute("pragma table_info(tasks)")}
    run_columns = {row[1] for row in upgraded.execute("pragma table_info(runs)")}
    assert {"program", "priority", "privacy"} <= project_columns
    assert {"risk", "complexity", "acceptance", "allowed_paths", "budget"} <= task_columns
    assert {"workspace_path", "command_json", "usage_json", "exit_code"} <= run_columns


def test_health_detects_dirty_repo(conn, project, git_repo):
    (git_repo / "new.txt").write_text("untracked\n", encoding="utf-8")
    row = health.inspect_project(conn, project)
    assert row.health == "attention"
    assert row.untracked == 1
    assert any("dirty worktree" in warning for warning in row.warnings)


def test_digest_renderer_has_decision_sections(conn, project):
    rows = health.inspect_portfolio(conn)
    rendered = health.render_digest(rows)
    assert "# Cortex daily digest" in rendered
    assert "## Ready for review" in rendered
    assert "## Needs attention" in rendered
    assert "## Summary" in rendered


def test_routing_keeps_low_risk_docs_local(conn, project):
    tid = store.create_task(
        conn,
        project_id=project["id"],
        title="Summarize README files",
        type="docs",
        acceptance="Return ten bullets",
    )
    route = routing.route_task(project, store.get_task(conn, tid))
    assert route.worker == "ollama"
    assert route.model == "phi4:14b"
    assert route.risk == "low"


def test_routing_escalates_production_billing(conn, project):
    tid = store.create_task(
        conn,
        project_id=project["id"],
        title="Deploy Stripe billing migration to production",
        type="code",
        acceptance="Webhook test passes",
    )
    route = routing.route_task(project, store.get_task(conn, tid))
    assert route.worker == "codex"
    assert route.risk == "high"
    assert route.requires_approval is True


@pytest.mark.parametrize("worker", ["codex", "claude", "gemini", "grok", "ollama"])
def test_worker_commands_do_not_embed_full_brief(worker, tmp_path):
    spec = workers.build_command(
        worker=worker,
        model="default" if worker != "ollama" else "phi4:14b",
        action="review",
        workspace=tmp_path,
        brief_path=tmp_path / "brief.md",
        budget="small",
    )
    assert "TOP SECRET TASK BODY" not in spec.display


def test_worktree_refuses_dirty_canonical_repo(git_repo):
    (git_repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(worktrees.WorktreeError, match="dirty"):
        worktrees.ensure(git_repo, "demo", "task1")


def test_dispatch_preview_is_non_mutating(conn, project, git_repo):
    tid = store.create_task(
        conn,
        project_id=project["id"],
        title="Review the README",
        type="review",
        acceptance="Return findings only",
    )
    task = store.get_task(conn, tid)
    planned = dispatcher.preview(conn, task, worker_override="ollama")
    assert planned.command is not None
    assert not (config.run_root() / tid).exists()
    assert store.get_task(conn, tid)["status"] == "open"


def test_local_evidence_bundle_is_bounded_and_skips_env(git_repo):
    (git_repo / "PROJECT-STATUS.md").write_text("# Current\nship it\n", encoding="utf-8")
    (git_repo / ".env").write_text("SECRET=do-not-read\n", encoding="utf-8")
    text = evidence.bundle(git_repo, patterns_text="PROJECT-STATUS.md", max_chars=1000)
    assert "ship it" in text
    assert "do-not-read" not in text
    assert len(text) <= 1200


def test_read_only_dispatch_captures_result(conn, project, monkeypatch):
    tid = store.create_task(
        conn,
        project_id=project["id"],
        title="Summarize the project",
        type="docs",
        acceptance="Return a concise summary",
    )

    def fake_execute(spec, *, workspace, brief, timeout):
        return workers.WorkerResult(0, '{"response":"done","stats":{"tokens":12}}', "", {"tokens": 12})

    monkeypatch.setattr(workers, "execute", fake_execute)
    result = dispatcher.dispatch(
        conn, store.get_task(conn, tid), worker_override="ollama", timeout=30
    )
    assert result.task_status == "done"
    assert store.get_task(conn, tid)["status"] == "done"


def test_worker_execute_requests_utf8(monkeypatch, tmp_path):
    captured = {}

    class Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return Proc()

    monkeypatch.setattr(workers.shutil, "which", lambda _: "worker.exe")
    monkeypatch.setattr(workers.subprocess, "run", fake_run)
    spec = workers.CommandSpec("codex", ("codex", "exec", "-"), True)
    workers.execute(spec, workspace=tmp_path, brief="hello", timeout=30)
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def test_ollama_worker_uses_local_http_api(monkeypatch, tmp_path):
    captured = {}

    def fake_generate(prompt, *, model, timeout):
        captured.update(prompt=prompt, model=model, timeout=timeout)
        return "clean response"

    monkeypatch.setattr(workers.ollama_client, "generate", fake_generate)
    spec = workers.CommandSpec("ollama", ("ollama", "run", "phi4:14b"), True)
    result = workers.execute(spec, workspace=tmp_path, brief="hello", timeout=30)
    assert result.stdout == "clean response"
    assert captured == {"prompt": "hello", "model": "phi4:14b", "timeout": 30.0}


def test_write_dispatch_uses_worktree_and_checks_paths(
    conn, project, git_repo, monkeypatch
):
    store.update_project(
        conn, project["id"], test_command='python -c "raise SystemExit(0)"'
    )
    project = store.get_project(conn, project["id"])
    tid = store.create_task(
        conn,
        project_id=project["id"],
        title="Add a bounded code file",
        type="code",
        risk="low",
        complexity=4,
        acceptance="Tests pass",
        allowed_paths="src/*",
    )

    def fake_execute(spec, *, workspace, brief, timeout):
        target = Path(workspace) / "src" / "feature.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
        return workers.WorkerResult(0, "done", "", None)

    monkeypatch.setattr(workers, "execute", fake_execute)
    result = dispatcher.dispatch(
        conn,
        store.get_task(conn, tid),
        worker_override="gemini",
        action_override="implement",
        allow_write=True,
        timeout=30,
    )
    assert result.task_status == "review"
    assert result.workspace != git_repo
    assert result.tests_passed == 1
    assert result.violations == ()
    assert "src/feature.py" in result.files_changed
