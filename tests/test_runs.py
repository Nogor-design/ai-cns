"""The run loop: agentic capture, API capture, manual capture, outcome resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cortex import runs as runs_mod, store


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


# ----------------------------------------------------------------- agentic ---
def test_agentic_start_records_git_before_and_writes_brief(conn, project):
    tid = store.create_task(conn, project_id=project["id"], title="t", type="code")
    task = store.get_task(conn, tid)
    started = runs_mod.start_run(
        conn, task, mode="agentic_cli", model="codex", use_ollama=False
    )
    run = store.get_run(conn, started.run_id)
    assert run["git_before"] is not None
    assert run["execution_mode"] == "agentic_cli"
    assert run["captured_via"] == "git"
    # task is now in_progress with the brief stored
    assert store.get_task(conn, tid)["status"] == "in_progress"


def test_agentic_done_captures_working_tree_changes(conn, project):
    repo = Path(project["repo_path"])
    tid = store.create_task(conn, project_id=project["id"], title="t", type="code")
    task = store.get_task(conn, tid)
    started = runs_mod.start_run(conn, task, mode="agentic_cli", model="codex", use_ollama=False)

    # The "agent" edits a tracked file and adds an untracked one (no commit).
    (repo / "README.md").write_text("# hello\nmore lines\n", encoding="utf-8")
    (repo / "new.py").write_text("print('hi')\n", encoding="utf-8")

    run = store.get_run(conn, started.run_id)
    result = runs_mod.finish_agentic(conn, run, run_tests=False)
    assert "README.md" in result.files_changed
    assert "new.py" in result.files_changed
    # diff_size must account for both edited tracked files and new untracked ones.
    assert result.diff_size > 0
    assert result.tests_passed is None
    assert store.get_run(conn, started.run_id)["ended_at"] is not None


def test_agentic_runs_test_command(conn, git_repo):
    pid = store.create_project(
        conn, name="Tested", repo_path=str(git_repo),
        test_command="python -c \"raise SystemExit(0)\"",
    )
    proj = store.get_project(conn, pid)
    tid = store.create_task(conn, project_id=pid, title="t", type="code")
    task = store.get_task(conn, tid)
    started = runs_mod.start_run(conn, task, mode="agentic_cli", model="codex", use_ollama=False)
    result = runs_mod.finish_agentic(conn, store.get_run(conn, started.run_id))
    assert result.tests_passed == 1


def test_agentic_failing_test_command(conn, git_repo):
    pid = store.create_project(
        conn, name="Failing", repo_path=str(git_repo),
        test_command="python -c \"raise SystemExit(1)\"",
    )
    proj = store.get_project(conn, pid)
    tid = store.create_task(conn, project_id=pid, title="t", type="code")
    task = store.get_task(conn, tid)
    started = runs_mod.start_run(conn, task, mode="agentic_cli", model="codex", use_ollama=False)
    result = runs_mod.finish_agentic(conn, store.get_run(conn, started.run_id))
    assert result.tests_passed == 0


# -------------------------------------------------------- outcome resolution ---
def test_resolve_survived(conn, project):
    repo = Path(project["repo_path"])
    tid = store.create_task(conn, project_id=project["id"], title="t", type="code")
    task = store.get_task(conn, tid)
    started = runs_mod.start_run(conn, task, mode="agentic_cli", model="codex", use_ollama=False)
    # Agent commits its work.
    _commit(repo, "feature.py", "x = 1\n")
    runs_mod.finish_agentic(conn, store.get_run(conn, started.run_id), run_tests=False)
    # A later commit happens on top — the run's commit survives in history.
    _commit(repo, "later.py", "y = 2\n")
    results = runs_mod.resolve_outcomes(conn, project)
    assert results[started.run_id] == "survived"
    assert store.get_run(conn, started.run_id)["outcome"] == "survived"


def test_resolve_reverted(conn, project):
    repo = Path(project["repo_path"])
    before = _git(repo, "rev-parse", "HEAD")
    tid = store.create_task(conn, project_id=project["id"], title="t", type="code")
    task = store.get_task(conn, tid)
    started = runs_mod.start_run(conn, task, mode="agentic_cli", model="codex", use_ollama=False)
    _commit(repo, "doomed.py", "x = 1\n")
    runs_mod.finish_agentic(conn, store.get_run(conn, started.run_id), run_tests=False)
    # The commit is discarded (hard reset back to before).
    _git(repo, "reset", "--hard", before)
    results = runs_mod.resolve_outcomes(conn, project)
    assert results[started.run_id] == "reverted"


# --------------------------------------------------------------------- api ---
def test_api_mode_stores_response_with_injected_caller(conn, project):
    tid = store.create_task(conn, project_id=project["id"], title="summarize", type="research")
    task = store.get_task(conn, tid)
    calls = {}

    def fake_caller(model: str, brief: str) -> str:
        calls["model"] = model
        calls["brief"] = brief
        return "the answer is 42"

    started = runs_mod.start_run(
        conn, task, mode="api", model="gemini-1.5-pro",
        use_ollama=False, api_caller=fake_caller,
    )
    assert started.response == "the answer is 42"
    run = store.get_run(conn, started.run_id)
    assert run["response"] == "the answer is 42"
    assert run["ended_at"] is not None
    assert calls["model"] == "gemini-1.5-pro"
    assert store.get_task(conn, tid)["status"] == "done"


# ------------------------------------------------------------------ manual ---
def test_manual_capture(conn, project):
    tid = store.create_task(conn, project_id=project["id"], title="ask", type="other")
    task = store.get_task(conn, tid)
    started = runs_mod.start_run(conn, task, mode="manual", model="some-web-model", use_ollama=False)
    assert started.response is None
    run = store.get_run(conn, started.run_id)
    runs_mod.capture_manual(conn, run, "pasted answer", note="looked good")
    saved = store.get_run(conn, started.run_id)
    assert saved["response"] == "pasted answer"
    assert saved["human_note"] == "looked good"
    assert store.get_task(conn, tid)["status"] == "done"


def test_unknown_mode_raises(conn, project):
    tid = store.create_task(conn, project_id=project["id"], title="t")
    task = store.get_task(conn, tid)
    with pytest.raises(ValueError):
        runs_mod.start_run(conn, task, mode="telepathy", model=None, use_ollama=False)
