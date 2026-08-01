"""The context compiler."""

from __future__ import annotations

import pytest

from cortex import brief as brief_mod, config, state as state_mod, store


def test_brief_without_task_uses_scaffold(conn, project):
    # No state.md on disk yet -> compiler synthesizes a scaffold.
    compiled = brief_mod.compile_brief(conn, project, use_ollama=False)
    assert "Demo Project — State" in compiled.text
    assert "## Goal (now)" in compiled.text
    assert compiled.mode == "manual"


def test_brief_with_task_prepends_spec(conn, project):
    tid = store.create_task(
        conn, project_id=project["id"], title="Add caching", type="code",
        brief="Implement an LRU cache.",
    )
    task = store.get_task(conn, tid)
    compiled = brief_mod.compile_brief(
        conn, project, task=task, model="codex", mode="agentic_cli", use_ollama=False
    )
    assert "# Task: Add caching" in compiled.text
    assert "_Type: code_" in compiled.text
    assert "Implement an LRU cache." in compiled.text
    assert compiled.task_id == tid


def test_agentic_mode_writes_last_brief(conn, project):
    brief_mod.compile_brief(conn, project, mode="agentic_cli", use_ollama=False)
    path = config.last_brief_path(project["repo_path"])
    assert path.exists()
    assert "Demo Project" in path.read_text(encoding="utf-8")


def test_agentic_includes_test_command(conn, git_repo):
    pid = store.create_project(
        conn, name="With Tests", repo_path=str(git_repo), test_command="pytest -q"
    )
    proj = store.get_project(conn, pid)
    compiled = brief_mod.compile_brief(conn, proj, mode="agentic_cli", use_ollama=False)
    assert "pytest -q" in compiled.text


def test_secret_in_state_blocks_brief(conn, project):
    # Inject a secret into the on-disk state.md.
    state_mod.write_state(
        project["repo_path"],
        "# Demo\n## Goal (now)\nuse key sk-abcdefghijklmnopqrstuvwxyz0123\n",
    )
    with pytest.raises(brief_mod.SecretsDetected) as exc:
        brief_mod.compile_brief(conn, project, use_ollama=False)
    assert any(f.kind == "openai_api_key" for f in exc.value.findings)
