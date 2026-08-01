"""End-to-end CLI tests via Typer's CliRunner (uses the isolated CORTEX_DB)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from cortex.cli import app

runner = CliRunner()


def _run(*args: str):
    return runner.invoke(app, list(args))


def test_init_then_project_list(git_repo):
    r = _run("init", str(git_repo), "--name", "MyApp", "--stack", "Python", "--goal", "ship")
    assert r.exit_code == 0, r.output
    assert "registered project 'myapp'" in r.output
    assert (git_repo / ".cortex" / "state.md").exists()

    r = _run("project", "list")
    assert "myapp" in r.output


def test_init_rejects_missing_path(tmp_path):
    r = _run("init", str(tmp_path / "does-not-exist"))
    assert r.exit_code == 1
    assert "does not exist" in r.output


def test_full_agentic_flow(git_repo):
    assert _run("init", str(git_repo), "--name", "Flow").exit_code == 0
    r = _run("task", "add", "flow", "Add feature", "--type", "code")
    assert r.exit_code == 0, r.output
    task_id = r.output.strip().splitlines()[-1]

    r = _run("run", task_id, "--mode", "agentic_cli", "--model", "codex", "--no-ollama")
    assert r.exit_code == 0, r.output
    assert "started (agentic_cli)" in r.output
    run_id = r.output.split("run ")[1].split()[0]
    assert (git_repo / ".cortex" / "last_brief.md").exists()

    # Simulate the agent committing work.
    (git_repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "feat"], check=True)

    r = _run("done", run_id, "--no-tests")
    assert r.exit_code == 0, r.output
    assert "closed" in r.output
    assert "feature.py" in r.output

    r = _run("resolve", "flow")
    assert r.exit_code == 0, r.output
    assert run_id in r.output and "survived" in r.output

    r = _run("history", "flow", "--type", "code")
    assert r.exit_code == 0, r.output
    assert "codex" in r.output


def test_brief_blocks_on_secret(git_repo):
    assert _run("init", str(git_repo), "--name", "Sec").exit_code == 0
    state = git_repo / ".cortex" / "state.md"
    state.write_text(
        state.read_text(encoding="utf-8") + "\nleaked AKIAIOSFODNN7EXAMPLE\n",
        encoding="utf-8",
    )
    r = _run("brief", "sec", "--no-ollama")
    assert r.exit_code == 2
    assert "BLOCKED" in r.output
    assert "aws_access_key_id" in r.output


def test_decide_and_state_regen(git_repo):
    assert _run("init", str(git_repo), "--name", "Dec").exit_code == 0
    r = _run("decide", "dec", "Use token bucket", "--why", "simplest correct")
    assert r.exit_code == 0, r.output
    r = _run("state", "dec", "--regen", "--no-ollama")
    assert r.exit_code == 0, r.output
    assert "Use token bucket" in r.output


def test_manual_capture_via_file(git_repo, tmp_path):
    assert _run("init", str(git_repo), "--name", "Man").exit_code == 0
    r = _run("task", "add", "man", "Ask a thing")
    task_id = r.output.strip().splitlines()[-1]
    r = _run("run", task_id, "--mode", "manual", "--model", "web-model", "--no-ollama")
    assert r.exit_code == 0, r.output
    run_id = r.output.split("run ")[1].split()[0]

    resp = tmp_path / "resp.txt"
    resp.write_text("the pasted answer", encoding="utf-8")
    r = _run("capture", run_id, "--file", str(resp))
    assert r.exit_code == 0, r.output
    assert "captured response" in r.output


def test_history_empty(git_repo):
    assert _run("init", str(git_repo), "--name", "Empty").exit_code == 0
    r = _run("history", "empty")
    assert r.exit_code == 0
    assert "no runs recorded" in r.output
