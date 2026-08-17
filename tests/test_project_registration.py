from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cortex import project_registration, store


def test_folder_picker_returns_a_valid_absolute_directory(tmp_path, monkeypatch):
    repo = tmp_path / "picked-project"
    repo.mkdir()
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=f"{repo}\n", stderr="")

    monkeypatch.setattr(project_registration.subprocess, "run", fake_run)

    selected = project_registration.choose_project_folder(str(tmp_path))

    assert selected == str(repo.resolve())
    assert captured["command"][:2] == [project_registration.sys.executable, "-c"]
    assert captured["kwargs"]["env"]["CORTEX_FOLDER_PICKER_INITIAL_PATH"] == str(
        tmp_path.resolve()
    )
    assert "shell" not in captured["kwargs"]


def test_folder_picker_cancel_keeps_manual_entry_available(monkeypatch):
    monkeypatch.setattr(
        project_registration.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )

    assert project_registration.choose_project_folder() is None


def test_preview_detects_stack_test_command_and_existing_state(conn, tmp_path):
    repo = tmp_path / "guided-app"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "package.json").write_text(json.dumps({
        "scripts": {"test": "vitest run"},
        "dependencies": {"react": "latest", "vite": "latest"},
    }), encoding="utf-8")
    state_path = repo / ".cortex" / "state.md"
    state_path.parent.mkdir()
    state_path.write_text("# Existing state\n", encoding="utf-8")

    result = project_registration.preview(conn, str(repo))

    assert result["repo_path"] == str(repo.resolve())
    assert result["suggested_project_id"] == "guided-app"
    assert result["stack"] == "TypeScript, React, Vite"
    assert result["test_command"] == "npm test"
    assert result["is_git"] is True
    assert result["state_exists"] is True
    assert result["already_registered"] is False


def test_register_is_attributed_and_preserves_an_existing_state(conn, tmp_path):
    repo = tmp_path / "private-product"
    repo.mkdir()
    state_path = repo / ".cortex" / "state.md"
    state_path.parent.mkdir()
    state_path.write_text("# Owner state\n", encoding="utf-8")

    result = project_registration.register(conn, {
        "repo_path": str(repo),
        "name": "Private Product",
        "program": "revenue products",
        "priority": 1,
        "privacy": "restricted",
        "stack": "Python",
        "current_goal": "Reach a paid pilot",
        "test_command": "python -m pytest -q",
        "allowed_workers": ["ollama"],
        "track_state": True,
    })

    project = result["project"]
    assert project["id"] == "private-product"
    assert project["program"] == "revenue-products"
    assert json.loads(project["allowed_workers"]) == ["ollama"]
    assert result["state_created"] is False
    assert result["state_preserved"] is True
    assert state_path.read_text(encoding="utf-8") == "# Owner state\n"
    event = store.list_activity_events(conn, project["id"])[0]
    assert event["action"] == "project.created"
    assert event["actor_type"] == "human"
    assert event["actor_name"] == "owner"
    assert event["source"] == "dashboard"


def test_register_rolls_back_when_state_creation_fails(conn, tmp_path, monkeypatch):
    repo = tmp_path / "unwritable-product"
    repo.mkdir()

    def fail_write(*_args, **_kwargs):
        raise OSError("state write failed")

    monkeypatch.setattr(project_registration.state_mod, "write_initial_state", fail_write)
    with pytest.raises(OSError, match="state write failed"):
        project_registration.register(conn, {
            "repo_path": str(repo),
            "name": "Unwritable Product",
            "allowed_workers": ["ollama"],
            "track_state": True,
        })

    assert conn.execute(
        "SELECT 1 FROM projects WHERE id = 'unwritable-product'"
    ).fetchone() is None


def test_register_rejects_duplicate_folder_even_with_another_name(conn, tmp_path):
    repo = tmp_path / "one-folder"
    repo.mkdir()
    store.create_project(conn, name="First", repo_path=str(repo.resolve()))

    with pytest.raises(ValueError, match="already registered"):
        project_registration.register(conn, {
            "repo_path": str(repo),
            "name": "Second",
            "allowed_workers": ["ollama"],
            "track_state": False,
        })


@pytest.mark.parametrize(
    ("name", "track_state", "message"),
    [
        ("---", False, "letters or numbers"),
        ("Valid name", "false", "true or false"),
    ],
)
def test_register_rejects_ambiguous_registration_values(
    conn, tmp_path, name, track_state, message
):
    repo = tmp_path / "invalid-registration"
    repo.mkdir()

    with pytest.raises(ValueError, match=message):
        project_registration.register(conn, {
            "repo_path": str(repo),
            "name": name,
            "allowed_workers": ["ollama"],
            "track_state": track_state,
        })
