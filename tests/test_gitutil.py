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
