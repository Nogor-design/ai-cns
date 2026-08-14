"""State document scaffold, deterministic regen, and section preservation."""

from __future__ import annotations

from cortex import state as state_mod, store


def test_scaffold_has_all_sections(project):
    md = state_mod.scaffold(project)
    for heading in [
        "## Goal (now)", "## Stack", "## Important files", "## Open tasks",
        "## Recent decisions", "## Known risks / assumptions",
        "## Definition of done",
    ]:
        assert heading in md
    assert "ship it" in md  # current_goal carried through


def test_regen_deterministic_includes_open_tasks_and_decisions(conn, project):
    task_id = store.create_task(
        conn, project_id=project["id"], title="Wire the API", type="code",
        assignee="codex", next_action="Run the contract tests",
    )
    store.update_task(conn, task_id, status="running", progress=40)
    store.create_decision(
        conn, project_id=project["id"], decision="pick sqlite", rationale="zero config"
    )
    content = state_mod.regen(conn, project, use_ollama=False)
    assert "Wire the API" in content
    assert "pick sqlite" in content
    assert "owner: codex" in content
    assert "progress: 40%" in content
    assert "next: Run the contract tests" in content
    assert "## Recent activity" in content
    # Written to disk too.
    assert state_mod.read_state(project["repo_path"]) == content


def test_regen_preserves_human_sections(conn, project):
    state_mod.write_state(
        project["repo_path"],
        "# Demo\n"
        "## Important files\n- src/main.py — entrypoint\n\n"
        "## Known risks / assumptions\n- rate limits unknown\n\n"
        "## Definition of done\n- ships\n",
    )
    content = state_mod.regen(conn, project, use_ollama=False)
    assert "src/main.py — entrypoint" in content
    assert "rate limits unknown" in content
    assert "- ships" in content


def test_extract_sections_handles_none():
    assert state_mod._extract_sections(None, ["Goal"]) == {}
