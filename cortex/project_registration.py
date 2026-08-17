"""Guarded local project discovery and registration for the dashboard."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import config, ids, policy, state as state_mod, store

MAX_METADATA_BYTES = 1_000_000
FOLDER_PICKER_TIMEOUT_SECONDS = 300

_FOLDER_PICKER_SCRIPT = r"""
import os
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
initial = os.environ.get("CORTEX_FOLDER_PICKER_INITIAL_PATH") or None
try:
    selected = filedialog.askdirectory(
        parent=root,
        title="Choose a project folder",
        initialdir=initial,
        mustexist=True,
    )
    print(selected or "", end="")
finally:
    root.destroy()
"""


def resolve_project_path(value: Any) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("project folder is required")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("project folder must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"project folder does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise ValueError(f"project folder is not a directory: {resolved}")
    return resolved


def choose_project_folder(initial_path: Any = None) -> str | None:
    """Open a local native directory chooser and return one absolute folder path."""
    environment = os.environ.copy()
    raw_initial = str(initial_path or "").strip()
    if raw_initial:
        try:
            candidate = Path(raw_initial).expanduser()
            if candidate.is_absolute() and candidate.is_dir():
                environment["CORTEX_FOLDER_PICKER_INITIAL_PATH"] = str(
                    candidate.resolve()
                )
        except (OSError, RuntimeError):
            pass
    environment.setdefault("PYTHONIOENCODING", "utf-8")

    try:
        completed = subprocess.run(
            [sys.executable, "-c", _FOLDER_PICKER_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FOLDER_PICKER_TIMEOUT_SECONDS,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            "Folder picker could not be opened. Enter the full path manually."
        ) from exc

    if completed.returncode != 0:
        raise ValueError(
            "Folder picker is unavailable on this computer. Enter the full path manually."
        )
    selected = completed.stdout.strip()
    if not selected:
        return None
    return str(resolve_project_path(selected))


def _path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve())).casefold()


def _small_text(path: Path) -> str:
    try:
        if not path.is_file() or path.stat().st_size > MAX_METADATA_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _package_details(repo: Path) -> tuple[list[str], str | None]:
    package_file = repo / "package.json"
    if not package_file.is_file() or package_file.stat().st_size > MAX_METADATA_BYTES:
        return [], None
    try:
        package = json.loads(package_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["Node.js"], None
    dependencies = {
        str(key).lower()
        for section in ("dependencies", "devDependencies")
        for key in (package.get(section) or {})
    }
    stack = ["TypeScript" if (repo / "tsconfig.json").exists() else "JavaScript"]
    for dependency, label in (
        ("next", "Next.js"), ("react", "React"), ("vite", "Vite"),
        ("vue", "Vue"), ("svelte", "Svelte"), ("express", "Express"),
    ):
        if dependency in dependencies:
            stack.append(label)
    scripts = package.get("scripts") or {}
    test_script = str(scripts.get("test") or "").strip()
    if not test_script or "no test specified" in test_script.lower():
        return stack, None
    manager = "pnpm" if (repo / "pnpm-lock.yaml").exists() else (
        "yarn" if (repo / "yarn.lock").exists() else "npm"
    )
    return stack, f"{manager} test"


def detect_project(repo: Path) -> dict[str, Any]:
    stack, test_command = _package_details(repo)
    detected_files: list[str] = []
    for name in (
        "package.json", "pyproject.toml", "requirements.txt", "setup.py",
        "Cargo.toml", "go.mod", "Gemfile", "composer.json", "Dockerfile",
    ):
        if (repo / name).exists():
            detected_files.append(name)

    python_markers = [repo / "pyproject.toml", repo / "requirements.txt", repo / "setup.py"]
    if any(path.exists() for path in python_markers):
        stack.append("Python")
        python_evidence = "\n".join(_small_text(path) for path in python_markers)
        if test_command is None and (
            "pytest" in python_evidence.lower() or (repo / "tests").is_dir()
        ):
            test_command = "python -m pytest -q"
    if (repo / "Cargo.toml").exists():
        stack.append("Rust")
        test_command = test_command or "cargo test"
    if (repo / "go.mod").exists():
        stack.append("Go")
        test_command = test_command or "go test ./..."
    if list(repo.glob("*.sln")) or list(repo.glob("*.csproj")):
        stack.append(".NET")
        test_command = test_command or "dotnet test"
    if (repo / "Dockerfile").exists():
        stack.append("Docker")

    unique_stack = list(dict.fromkeys(stack))
    state_path = config.state_path(repo)
    return {
        "repo_path": str(repo),
        "suggested_name": repo.name,
        "suggested_project_id": ids.slugify(repo.name),
        "stack": ", ".join(unique_stack) or None,
        "test_command": test_command,
        "is_git": (repo / ".git").exists(),
        "detected_files": detected_files,
        "state_path": str(state_path),
        "state_exists": state_path.is_file(),
    }


def preview(conn: sqlite3.Connection, repo_path: Any) -> dict[str, Any]:
    repo = resolve_project_path(repo_path)
    result = detect_project(repo)
    existing = next(
        (
            row for row in store.list_projects(conn)
            if _path_key(row["repo_path"]) == _path_key(repo)
        ),
        None,
    )
    suggested_id_exists = conn.execute(
        "SELECT 1 FROM projects WHERE id = ?", (result["suggested_project_id"],)
    ).fetchone() is not None
    result.update({
        "already_registered": existing is not None,
        "existing_project_id": existing["id"] if existing else None,
        "suggested_id_exists": suggested_id_exists,
        "default_privacy": "internal",
        "default_workers": list(policy.DEFAULT_BY_PRIVACY["internal"]),
    })
    return result


def _bounded(value: Any, label: str, *, required: bool = False, limit: int = 500) -> str | None:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{label} is required")
    if len(text) > limit:
        raise ValueError(f"{label} must be {limit} characters or fewer")
    return text or None


def register(conn: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    repo = resolve_project_path(body.get("repo_path"))
    if any(_path_key(row["repo_path"]) == _path_key(repo) for row in store.list_projects(conn)):
        raise ValueError("this project folder is already registered")

    name = _bounded(body.get("name"), "project name", required=True, limit=120)
    assert name is not None
    if not any(character.isalnum() for character in name):
        raise ValueError("project name must contain letters or numbers")
    project_id = ids.slugify(name)
    if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
        raise ValueError(f"project id '{project_id}' already exists; choose a different name")

    privacy = str(body.get("privacy") or "internal").strip().lower()
    if privacy not in store.PRIVACY_LEVELS:
        raise ValueError("privacy must be public, internal, or restricted")
    try:
        priority = int(body.get("priority", 3))
    except (TypeError, ValueError) as exc:
        raise ValueError("priority must be between 1 and 5") from exc
    if priority < 1 or priority > 5:
        raise ValueError("priority must be between 1 and 5")

    workers = body.get("allowed_workers")
    if workers is None:
        workers = list(policy.DEFAULT_BY_PRIVACY[privacy])
    if not isinstance(workers, list):
        raise ValueError("allowed workers must be a list")
    policy.encode(workers)
    track_state = body.get("track_state", True)
    if not isinstance(track_state, bool):
        raise ValueError("track state must be true or false")
    state_path = config.state_path(repo)
    state_existed = state_path.is_file()
    state_created = False

    try:
        store.create_project(
            conn,
            name=name,
            repo_path=str(repo),
            stack=_bounded(body.get("stack"), "stack", limit=500),
            current_goal=_bounded(body.get("current_goal"), "current goal", limit=1000),
            test_command=_bounded(body.get("test_command"), "test command", limit=500),
            program=ids.slugify(_bounded(body.get("program"), "program", limit=100) or "general"),
            priority=priority,
            privacy=privacy,
            state_mode="tracked" if track_state else "deferred",
            project_id=project_id,
            allowed_workers=workers,
            actor_type="human",
            actor_name="owner",
            source="dashboard",
            record_activity=True,
            commit=False,
        )
        project = store.get_project(conn, project_id)
        if track_state and not state_existed:
            state_mod.write_initial_state(repo, state_mod.scaffold(project))
            state_created = True
        conn.commit()
    except Exception:
        conn.rollback()
        if state_created and state_path.is_file():
            state_path.unlink()
        raise

    return {
        "project": store.get_project(conn, project_id),
        "state_path": str(state_path) if track_state else None,
        "state_created": state_created,
        "state_preserved": track_state and state_existed,
    }
