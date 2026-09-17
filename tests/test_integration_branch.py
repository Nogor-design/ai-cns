"""The integration branch: created safely, merged --no-ff, never auto-resolved."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cortex import gitutil, integration


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _commit_on(repo: Path, branch: str, name: str, text: str) -> str:
    """Commit one file on a branch cut from the integration branch."""
    _git(repo, "worktree", "add", "-b", branch, str(repo.parent / branch.replace("/", "_")),
         integration.BRANCH)
    tree = repo.parent / branch.replace("/", "_")
    (tree / name).write_text(text, encoding="utf-8")
    _git(tree, "add", "-A")
    _git(tree, "-c", "user.email=a@b.c", "-c", "user.name=Agent", "commit", "-q", "-m", f"work: {name}")
    return gitutil.head(tree)


def test_ensure_creates_branch_and_private_workspace(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_WORK_ROOT", str(tmp_path / "work"))
    before_branch = gitutil.branch(git_repo)

    workspace = integration.ensure(git_repo, "demo")

    assert workspace.created is True
    assert workspace.branch == integration.BRANCH
    assert gitutil.branch(workspace.path) == integration.BRANCH
    # The owner's own checkout is untouched: same branch, still clean.
    assert gitutil.branch(git_repo) == before_branch
    assert gitutil.status_summary(git_repo).dirty == 0
    # Asking twice returns the same workspace rather than a second one.
    assert integration.ensure(git_repo, "demo").created is False


def test_merge_is_no_ff_and_revertible(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_WORK_ROOT", str(tmp_path / "work"))
    workspace = integration.ensure(git_repo, "demo")
    _commit_on(git_repo, "cortex/demo/t1", "feature.py", "value = 1\n")

    outcome = integration.merge(workspace, "cortex/demo/t1", message="cortex: merge t1")

    assert outcome.status == "merged"
    assert (workspace.path / "feature.py").exists()
    # --no-ff means the merge is one commit with two parents, so it reverts cleanly.
    parents = _git(workspace.path, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert len(parents) == 3

    reverted = integration.revert(workspace, outcome.commit)
    assert reverted.status == "merged"
    assert not (workspace.path / "feature.py").exists()


def test_conflicting_merge_is_aborted_and_reported(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_WORK_ROOT", str(tmp_path / "work"))
    workspace = integration.ensure(git_repo, "demo")
    _commit_on(git_repo, "cortex/demo/t1", "shared.py", "left = 1\n")
    _commit_on(git_repo, "cortex/demo/t2", "shared.py", "right = 2\n")
    assert integration.merge(workspace, "cortex/demo/t1", message="first").status == "merged"
    head_before = gitutil.head(workspace.path)

    outcome = integration.merge(workspace, "cortex/demo/t2", message="second")

    assert outcome.status == "conflict"
    assert "shared.py" in outcome.conflicts
    # Aborted, not left half-merged: HEAD and the working tree are as they were.
    assert gitutil.head(workspace.path) == head_before
    assert gitutil.status_summary(workspace.path).dirty == 0


def test_base_branch_is_never_a_merge_source(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_WORK_ROOT", str(tmp_path / "work"))
    workspace = integration.ensure(git_repo, "demo")

    with pytest.raises(integration.IntegrationError):
        integration.merge(workspace, workspace.base, message="no")
    with pytest.raises(integration.IntegrationError):
        integration.merge(workspace, "main", message="no")
    with pytest.raises(integration.IntegrationError):
        integration.merge(workspace, "cortex/demo/missing", message="no")


def test_refresh_fast_forwards_but_reports_divergence(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_WORK_ROOT", str(tmp_path / "work"))
    workspace = integration.ensure(git_repo, "demo")
    assert integration.refresh(workspace) == "up_to_date"

    # The owner moves the default branch on: integration should follow it.
    (git_repo / "owner.py").write_text("owner = True\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "owner work")
    assert integration.refresh(workspace) == "fast_forwarded"
    assert (workspace.path / "owner.py").exists()

    # Once Cortex work sits on integration, a further base commit diverges and
    # is left for a real merge rather than guessed at.
    _commit_on(git_repo, "cortex/demo/t1", "feature.py", "value = 1\n")
    integration.merge(workspace, "cortex/demo/t1", message="merge t1")
    (git_repo / "owner2.py").write_text("more = True\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "more owner work")
    assert integration.refresh(workspace) == "diverged"
    assert gitutil.status_summary(workspace.path).dirty == 0


def test_status_reports_ahead_behind_and_merges(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_WORK_ROOT", str(tmp_path / "work"))
    assert integration.status(git_repo, "demo")["exists"] is False

    workspace = integration.ensure(git_repo, "demo")
    _commit_on(git_repo, "cortex/demo/t1", "feature.py", "value = 1\n")
    integration.merge(workspace, "cortex/demo/t1", message="cortex: merge t1")

    info = integration.status(git_repo, "demo")
    assert info["exists"] is True
    assert info["ahead"] == 2  # the work commit and its merge commit
    assert info["behind"] == 0
    assert info["merges"][0]["subject"] == "cortex: merge t1"
