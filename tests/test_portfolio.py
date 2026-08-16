"""Portfolio health, routing, workers, worktrees, and dispatch safety."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from cortex import (
    config, dispatcher, evidence, git_monitor, health, jobs, policy, routing,
    runlog, store, workers, worktrees,
)


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
    assert {"program", "priority", "privacy", "state_mode"} <= project_columns
    assert {
        "risk", "complexity", "acceptance", "allowed_paths", "budget",
        "priority", "assignee", "requested_model", "effort", "due_at",
    } <= task_columns
    assert {"workspace_path", "command_json", "usage_json", "effort", "exit_code"} <= run_columns
    assert {"remote_url", "github_owner", "github_repo", "codex_project_id"} <= project_columns
    assert {
        "parent_id", "progress", "blocked_reason", "next_action", "codex_thread_id",
        "pm_session_id",
    } <= task_columns
    task_indexes = {
        row["name"]: row["unique"]
        for row in upgraded.execute("pragma index_list(tasks)").fetchall()
    }
    assert task_indexes["idx_tasks_github_issue_id"] == 1
    assert task_indexes["idx_tasks_github_project_item_id"] == 1
    assert upgraded.execute("pragma user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert upgraded.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='activity_events'"
    ).fetchone()


def test_early_v4_database_receives_final_pm_session_columns(tmp_path):
    path = tmp_path / "early-v4.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projects (
          id TEXT PRIMARY KEY, name TEXT, repo_path TEXT, stack TEXT,
          status TEXT, current_goal TEXT, test_command TEXT, updated_at TEXT
        );
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, project_id TEXT, title TEXT, type TEXT,
          status TEXT, brief TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE activity_events (
          id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT,
          actor_type TEXT, actor_name TEXT, action TEXT, summary TEXT,
          source TEXT, occurred_at TEXT, evidence_json TEXT
        );
        PRAGMA user_version = 4;
        """
    )
    conn.close()

    from cortex import db

    upgraded = db.connect(path)
    task_columns = {row[1] for row in upgraded.execute("pragma table_info(tasks)")}
    activity_columns = {
        row[1] for row in upgraded.execute("pragma table_info(activity_events)")
    }
    assert "pm_session_id" in task_columns
    assert "session_id" in activity_columns
    assert upgraded.execute("pragma user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_health_detects_dirty_repo(conn, project, git_repo):
    (git_repo / "new.txt").write_text("untracked\n", encoding="utf-8")
    row = health.inspect_project(conn, project)
    assert row.health == "attention"
    assert row.untracked == 1
    assert any("dirty worktree" in warning for warning in row.warnings)


def test_deferred_state_does_not_create_false_health_warning(conn, project):
    store.update_project(conn, project["id"], state_mode="deferred")
    row = health.inspect_project(conn, store.get_project(conn, project["id"]))
    assert "missing .cortex/state.md" not in row.warnings


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


def test_routing_uses_grok_for_adversarial_review(conn, project):
    tid = store.create_task(
        conn,
        project_id=project["id"],
        title="Adversarially review routing blind spots",
        type="review",
        acceptance="Rank five failure modes",
    )
    route = routing.route_task(project, store.get_task(conn, tid))
    assert route.worker == "grok"
    assert route.effort in {"low", "medium", "high", "xhigh"}


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


def test_worker_commands_include_supported_effort(tmp_path):
    codex = workers.build_command(
        worker="codex", model="default", action="review", workspace=tmp_path,
        brief_path=tmp_path / "brief.md", budget="small", effort="high",
    )
    claude = workers.build_command(
        worker="claude", model="default", action="review", workspace=tmp_path,
        brief_path=tmp_path / "brief.md", budget="small", effort="medium",
    )
    assert "model_reasoning_effort" in codex.display
    assert "--effort medium" in claude.display


def test_jsonl_usage_capture():
    stdout = '\n'.join([
        '{"type":"message"}',
        '{"type":"result","usage":{"input_tokens":120,"output_tokens":30}}',
    ])
    assert workers._extract_usage(stdout) == {"input_tokens": 120, "output_tokens": 30}


def test_git_monitor_persists_snapshot(conn, project):
    rows = git_monitor.refresh_portfolio(conn, fetch=False)
    assert rows[0]["project_id"] == project["id"]
    stored = store.list_git_checks(conn)
    assert stored[0]["project_id"] == project["id"]
    assert stored[0]["is_git"] == 1


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


def test_dispatch_honors_explicit_worker_assignment(conn, project):
    task_id = store.create_task(
        conn,
        project_id=project["id"],
        title="Review architecture decisions",
        type="review",
        complexity=5,
        assignee="codex",
    )
    planned = dispatcher.preview(conn, store.get_task(conn, task_id))
    assert planned.route.worker == "codex"
    assert planned.route.model == "default"


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

    def fake_execute(spec, *, workspace, brief, timeout, on_output=None):
        if on_output:
            on_output("streamed chunk\n")
        return workers.WorkerResult(0, '{"response":"done","stats":{"tokens":12}}', "", {"tokens": 12})

    monkeypatch.setattr(workers, "execute", fake_execute)
    result = dispatcher.dispatch(
        conn, store.get_task(conn, tid), worker_override="ollama", timeout=30
    )
    assert result.task_status == "done"
    assert store.get_task(conn, tid)["status"] == "done"


class _FakeProc:
    """Minimal stand-in for a streaming subprocess."""

    def __init__(self, stdout_text: str = "", stderr_text: str = "", returncode: int = 0):
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.stdin = io.StringIO()
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_worker_execute_requests_utf8(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return _FakeProc(stdout_text="ok\n")

    monkeypatch.setattr(workers.shutil, "which", lambda _: "worker.exe")
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)
    spec = workers.CommandSpec("codex", ("codex", "exec", "-"), True)
    workers.execute(spec, workspace=tmp_path, brief="hello", timeout=30)
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def test_worker_execute_streams_output_while_running(monkeypatch, tmp_path):
    """Output must reach the caller incrementally, not only at exit."""
    chunks: list[str] = []

    monkeypatch.setattr(workers.shutil, "which", lambda _: "worker.exe")
    monkeypatch.setattr(
        workers.subprocess, "Popen",
        lambda argv, **kwargs: _FakeProc(stdout_text="first\nsecond\n"),
    )
    spec = workers.CommandSpec("codex", ("codex", "exec", "-"), True)
    result = workers.execute(
        spec, workspace=tmp_path, brief="hello", timeout=30,
        on_output=chunks.append,
    )
    assert chunks == ["first\n", "second\n"]
    assert result.stdout == "first\nsecond\n"
    assert result.exit_code == 0


def test_worker_execute_survives_a_failing_output_callback(monkeypatch, tmp_path):
    """A logging problem must not take down the run itself."""
    monkeypatch.setattr(workers.shutil, "which", lambda _: "worker.exe")
    monkeypatch.setattr(
        workers.subprocess, "Popen",
        lambda argv, **kwargs: _FakeProc(stdout_text="payload\n"),
    )

    def explode(_text):
        raise RuntimeError("disk full")

    spec = workers.CommandSpec("codex", ("codex", "exec", "-"), True)
    result = workers.execute(
        spec, workspace=tmp_path, brief="hello", timeout=30, on_output=explode
    )
    assert result.stdout == "payload\n"
    assert result.exit_code == 0


def test_ollama_worker_uses_local_http_api(monkeypatch, tmp_path):
    captured = {}

    def fake_generate(prompt, *, model, timeout, on_chunk=None):
        captured.update(prompt=prompt, model=model, timeout=timeout)
        return "clean response", {"input_tokens": 4, "output_tokens": 2}

    monkeypatch.setattr(workers.ollama_client, "generate_with_usage", fake_generate)
    spec = workers.CommandSpec("ollama", ("ollama", "run", "phi4:14b"), True)
    result = workers.execute(spec, workspace=tmp_path, brief="hello", timeout=30)
    assert result.stdout == "clean response"
    assert result.usage == {"input_tokens": 4, "output_tokens": 2}
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

    def fake_execute(spec, *, workspace, brief, timeout, on_output=None):
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


def test_extract_text_unwraps_claude_json_envelope():
    payload = '{"is_error":false,"result":"Three blockers found.","usage":{"output_tokens":9}}'
    assert workers.extract_text(payload) == "Three blockers found."


def test_extract_text_takes_the_last_codex_agent_message():
    stream = (
        '{"type":"item.started","item":{"type":"reasoning"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"final answer"}}\n'
    )
    assert workers.extract_text(stream) == "final answer"


def test_extract_text_passes_plain_output_through():
    assert workers.extract_text("just prose\n") == "just prose\n"
    assert workers.extract_text("") == ""


def test_turn_budgets_allow_real_agentic_work():
    """A review spends most turns on tool calls before it can answer."""
    assert int(workers._max_turns("local")) >= 10
    assert int(workers._max_turns("small")) >= 20
    assert int(workers._max_turns("large")) > int(workers._max_turns("medium"))


def test_humanize_renders_claude_text_and_tool_calls():
    event = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "Checking the migration."},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "db/schema.sql"}},
        ]},
    })
    shown = workers.humanize("claude", event)
    assert "Checking the migration." in shown
    assert "→ Read(db/schema.sql)" in shown


def test_humanize_hides_protocol_noise_but_surfaces_failure():
    assert workers.humanize("claude", '{"type":"system","subtype":"init"}') is None
    assert workers.humanize("claude", '{"type":"result","is_error":false}') is None
    failed = workers.humanize(
        "claude", '{"type":"result","is_error":true,"terminal_reason":"max_turns"}'
    )
    assert "max_turns" in failed


def test_humanize_passes_plain_prose_through():
    assert workers.humanize("ollama", "a local model wrote this\n") == "a local model wrote this\n"


def test_extract_text_reads_a_stream_json_result():
    stream = (
        '{"type":"system","subtype":"init"}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"partial"}]}}\n'
        '{"type":"result","is_error":false,"result":"the final review","usage":{"output_tokens":5}}\n'
    )
    assert workers.extract_text(stream) == "the final review"
    assert workers._extract_usage(stream) == {"output_tokens": 5}
