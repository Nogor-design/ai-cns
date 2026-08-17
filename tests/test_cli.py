"""End-to-end CLI tests via Typer's CliRunner (uses the isolated CORTEX_DB)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from cortex import db, github_projects, store
from cortex import cli as cli_mod
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


def test_project_onboard_guides_new_project_through_exact_blueprint_approval(git_repo):
    user_input = "\n".join([
        "",  # detected project name
        "",  # internal privacy
        "",  # default worker allowlist
        "Operators need a trusted project handoff.",
        "The owner can verify the current delivery gate.",
        "Do not dispatch or complete tasks automatically.",
        "Repository evidence remains local during onboarding.",
        "No open decision currently.",
        "Trustworthy onboarding",
        "One approved blueprint and active phase are visible.",
        "Exact preview is approved",
        "y",
    ]) + "\n"

    result = runner.invoke(app, ["project", "onboard", str(git_repo)], input=user_input)

    assert result.exit_code == 0, result.output
    assert "No execution tasks were generated and no provider was contacted." in result.output
    assert "approved blueprint revision 1" in result.output
    assert (git_repo / ".cortex" / "blueprint.md").exists()
    with db.connect() as conn:
        project = store.list_projects(conn)[0]
        assert project["blueprint_status"] == "approved"
        tasks = store.list_tasks(conn, project["id"])
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Build project blueprint"
        assert tasks[0]["status"] == "done"


def test_project_onboard_resumes_saved_exact_review_without_reasking_questions(git_repo):
    assert _run("init", str(git_repo), "--name", "Resume Blueprint").exit_code == 0
    answers = "\n".join([
        "Operators need a restart-safe review.",
        "The exact saved preview can be approved later.",
        "Do not create tasks automatically.",
        "All discovery stays local.",
        "No open decision currently.",
        "Restart-safe approval",
        "The saved approval review survives a new CLI process.",
        "Exact preview is approved",
    ]) + "\n"
    prepared = runner.invoke(
        app,
        ["project", "onboard", str(git_repo), "--draft-only"],
        input=answers,
    )
    assert prepared.exit_code == 0, prepared.output
    assert "draft saved in review" in prepared.output
    assert not (git_repo / ".cortex" / "blueprint.md").exists()

    resumed = runner.invoke(
        app,
        ["project", "onboard", str(git_repo)],
        input="y\n",
    )
    assert resumed.exit_code == 0, resumed.output
    assert "Resuming the exact saved blueprint approval preview" in resumed.output
    assert "approved blueprint revision 1" in resumed.output
    assert (git_repo / ".cortex" / "blueprint.md").exists()


def test_project_worker_allowlist_can_be_inspected_and_updated(git_repo):
    r = _run(
        "init", str(git_repo), "--name", "Private App", "--privacy", "restricted",
        "--no-state",
    )
    assert r.exit_code == 0, r.output

    r = _run("project", "workers", "private-app")
    assert r.exit_code == 0, r.output
    assert "private-app: ollama" in r.output
    assert "default for privacy=restricted" in r.output

    r = _run("project", "workers", "private-app", "claude,ollama")
    assert r.exit_code == 0, r.output
    assert "may now use: claude, ollama" in r.output

    r = _run("project", "list")
    assert r.exit_code == 0, r.output
    assert "claude,ollama" in r.output


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


def test_pm_session_cli_records_attributed_activity(git_repo):
    assert _run("init", str(git_repo), "--name", "PM Flow").exit_code == 0
    started = _run(
        "pm", "start", "pm-flow", "Implement durable records",
        "--thread", "codex-thread-1", "--next-action", "Run tests",
    )
    assert started.exit_code == 0, started.output
    task_id = started.output.strip().splitlines()[-1].split()[-1]
    assert "PM session" in started.output

    event = _run(
        "pm", "event", task_id, "delegation.completed", "Gemini reviewed schema",
        "--actor", "gemini", "--model", "gemini-2.5-pro",
    )
    assert event.exit_code == 0, event.output
    closed = _run(
        "pm", "close", task_id, "Durable records accepted", "--status", "done",
        "--next-action", "Start Codex app integration",
    )
    assert closed.exit_code == 0, closed.output

    activity = _run("activity", "list", "--task", task_id, "--json")
    assert activity.exit_code == 0, activity.output
    assert "delegation.completed" in activity.output
    assert "pm.session_closed" in activity.output


def test_github_configure_and_plan_are_verified_read_only_surfaces(
    git_repo, isolated_db, monkeypatch
):
    assert _run("init", str(git_repo), "--name", "GitHub Mirror").exit_code == 0
    created = _run("task", "add", "github-mirror", "Mirror safely")
    task_id = created.output.strip().splitlines()[-1]

    configured_snapshot = {
        "project_id": "PVT_1",
        "project_title": "Engineering Mirror",
        "project_closed": False,
        "items": [],
        "field_schema": {},
    }
    monkeypatch.setattr(
        cli_mod.github_reader, "read_project",
        lambda owner, number: configured_snapshot,
    )
    configured = _run(
        "github", "configure", "github-mirror",
        "--owner", "example", "--number", "1",
    )
    assert configured.exit_code == 0, configured.output

    plan = github_projects.MirrorPlan(
        task_id=task_id,
        project_id="PVT_1",
        issue_id="I_1",
        project_item_id="PVTI_1",
        snapshot_digest="snapshot-digest",
        fingerprint="plan-v1:test-fingerprint",
        actions=(),
        conflicts=(),
    )
    strict_snapshot = {**configured_snapshot, "items_complete": True,
                       "field_schema_complete": True}
    monkeypatch.setattr(
        cli_mod.github_adapter, "prepare",
        lambda task, owner, number: (plan, strict_snapshot),
    )
    with db.connect(isolated_db) as conn:
        before_operations = conn.execute(
            "SELECT COUNT(*) FROM github_mirror_operations"
        ).fetchone()[0]
        before_task = dict(store.get_task(conn, task_id))

    result = _run("github", "plan", task_id, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["safe_to_apply"] is True
    assert payload["fingerprint"] == "plan-v1:test-fingerprint"

    with db.connect(isolated_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM github_mirror_operations"
        ).fetchone()[0] == before_operations
        assert dict(store.get_task(conn, task_id)) == before_task


def test_github_link_resolves_exact_issue_and_enforces_configured_repository(
    git_repo, isolated_db, monkeypatch
):
    assert _run("init", str(git_repo), "--name", "Issue Link").exit_code == 0
    assert _run(
        "project", "update", "issue-link",
        "--github-owner", "example", "--github-repo", "repo",
    ).exit_code == 0
    created = _run("task", "add", "issue-link", "Link me")
    task_id = created.output.strip().splitlines()[-1]
    monkeypatch.setattr(
        cli_mod.github_reader,
        "read_issue",
        lambda url: {
            "id": "I_exact", "number": 7, "url": url,
            "title": "Existing", "state": "OPEN", "repository": "example/repo",
        },
    )

    linked = _run(
        "github", "link", task_id, "https://github.com/example/repo/issues/7",
        "--actor", "codex",
    )
    assert linked.exit_code == 0, linked.output
    with db.connect(isolated_db) as conn:
        task = store.get_task(conn, task_id)
        assert task["github_issue_id"] == "I_exact"
        assert task["github_issue_number"] == 7
        assert any(
            event["action"] == "github.issue_linked"
            for event in store.list_activity_events(conn, task_id=task_id)
        )

    duplicate = _run(
        "github", "link", task_id, "https://github.com/example/repo/issues/7"
    )
    assert duplicate.exit_code == 1
    assert "already has a stable GitHub issue link" in duplicate.output
