"""Configuration and path resolution for Cortex-Lite.

The database lives at ~/.cortex/cortex.db by default, overridable via the
CORTEX_DB environment variable (handy for tests and isolated workspaces).
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_DB = "CORTEX_DB"
ENV_OLLAMA_HOST = "OLLAMA_HOST"
ENV_OLLAMA_MODEL = "CORTEX_OLLAMA_MODEL"
ENV_WORK_ROOT = "CORTEX_WORK_ROOT"

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "phi4:14b"

# Per-repo location of the canonical state document and last compiled brief.
CORTEX_DIR = ".cortex"
STATE_FILE = "state.md"
LAST_BRIEF_FILE = "last_brief.md"


def db_path() -> Path:
    """Resolve the SQLite database path, creating the parent dir if needed."""
    override = os.environ.get(ENV_DB)
    if override:
        p = Path(override).expanduser()
    else:
        p = Path.home() / ".cortex" / "cortex.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def ollama_host() -> str:
    return os.environ.get(ENV_OLLAMA_HOST, DEFAULT_OLLAMA_HOST)


def ollama_model() -> str:
    return os.environ.get(ENV_OLLAMA_MODEL, DEFAULT_OLLAMA_MODEL)


def cortex_dir(repo_path: str | os.PathLike[str]) -> Path:
    return Path(repo_path) / CORTEX_DIR


def state_path(repo_path: str | os.PathLike[str]) -> Path:
    return cortex_dir(repo_path) / STATE_FILE


def last_brief_path(repo_path: str | os.PathLike[str]) -> Path:
    return cortex_dir(repo_path) / LAST_BRIEF_FILE


def work_root() -> Path:
    """Root for isolated Git worktrees.

    Prefer the user's mostly-empty E: SSD on Windows when present. Tests and
    portable installs naturally fall back beside the configured Cortex DB.
    """
    override = os.environ.get(ENV_WORK_ROOT)
    if override:
        root = Path(override).expanduser()
    elif os.environ.get(ENV_DB):
        # Tests and portable installations that explicitly place the database
        # expect all runtime artifacts beside it.
        root = db_path().parent / "worktrees"
    elif os.name == "nt" and Path("E:/").exists():
        root = Path("E:/AI-Worktrees")
    else:
        root = db_path().parent / "worktrees"
    return root


def run_root() -> Path:
    return db_path().parent / "runs"
