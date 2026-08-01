"""Shared pytest fixtures: isolated DB, temp repos, and a git helper."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cortex import db, store


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point CORTEX_DB at a temp file so no test touches the real database."""
    dbfile = tmp_path / "cortex.db"
    monkeypatch.setenv("CORTEX_DB", str(dbfile))
    return dbfile


@pytest.fixture
def conn():
    c = db.connect()
    yield c
    c.close()


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo with one commit, configured for non-interactive use."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# hello\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def project(conn, git_repo):
    pid = store.create_project(
        conn,
        name="Demo Project",
        repo_path=str(git_repo),
        stack="Python",
        current_goal="ship it",
        test_command=None,
    )
    return store.get_project(conn, pid)
