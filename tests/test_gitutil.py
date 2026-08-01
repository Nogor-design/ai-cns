"""Git provenance helpers, including untracked-file accounting."""

from __future__ import annotations

import subprocess
from pathlib import Path

from cortex import gitutil


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_non_repo_is_safe(tmp_path):
    assert gitutil.is_repo(tmp_path) is False
    assert gitutil.head(tmp_path) is None
    assert gitutil.changed_files(tmp_path, None) == []
    assert gitutil.diff_size(tmp_path, None) == 0
    assert gitutil.dashboard_snapshot(tmp_path).is_git is False


def test_diff_size_counts_untracked_lines(git_repo):
    before = gitutil.head(git_repo)
    # New untracked file with 3 lines.
    (git_repo / "new.py").write_text("a\nb\nc\n", encoding="utf-8")
    files = gitutil.changed_files(git_repo, before)
    assert "new.py" in files
    assert gitutil.diff_size(git_repo, before) >= 3


def test_is_ancestor_and_commits_between(git_repo):
    before = gitutil.head(git_repo)
    (git_repo / "f.py").write_text("x=1\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "second")
    after = gitutil.head(git_repo)
    assert gitutil.is_ancestor(git_repo, before, after) is True
    assert gitutil.is_ancestor(git_repo, after, before) is False
    assert gitutil.commits_between(git_repo, before, after) == 1


def test_status_and_last_activity_are_scoped_inside_monorepo(git_repo):
    first = git_repo / "first"
    second = git_repo / "second"
    first.mkdir()
    second.mkdir()
    (first / "tracked.txt").write_text("one\n", encoding="utf-8")
    (second / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "add logical projects")
    (first / "local.txt").write_text("first\n", encoding="utf-8")
    (second / "local.txt").write_text("second\n", encoding="utf-8")

    assert gitutil.status_summary(first).untracked == 1
    assert gitutil.status_summary(second).untracked == 1
    assert gitutil.last_commit(first)[2] == "add logical projects"
    snapshot = gitutil.dashboard_snapshot(first)
    assert snapshot.is_git is True
    assert snapshot.summary.available is False
    assert snapshot.status_note == "Scoped Git status deferred for shared repository"
    assert snapshot.last_commit[2] == "Latest scoped file activity"
