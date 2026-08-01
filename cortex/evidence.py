"""Small, local evidence bundles for workers without repository tools."""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

from . import gitutil


_DEFAULT_DOCS = (
    "README.md",
    "PROJECT-STATUS.md",
    "ROADMAP.md",
    "PRODUCT_STRATEGY.md",
    "package.json",
    "pyproject.toml",
)
_SAFE_SUFFIXES = {".md", ".txt", ".json", ".toml", ".py", ".ts", ".tsx", ".js", ".mjs"}
_BLOCKED_NAMES = {".env", ".env.local", "secrets.toml", "credentials.json"}


def bundle(
    repo_path: str | Path,
    *,
    patterns_text: str | None = None,
    max_chars: int = 30_000,
    per_file_chars: int = 8_000,
) -> str:
    """Collect bounded text evidence for a local, non-tool-using model."""
    repo = Path(repo_path).resolve()
    patterns = _patterns(patterns_text) or list(_DEFAULT_DOCS)
    files = _matching_files(repo, patterns)

    parts = ["## Repository evidence (local, bounded)"]
    if gitutil.is_repo(repo):
        status = gitutil.status_summary(repo)
        parts.append(
            f"Git: branch={gitutil.branch(repo) or '?'}; "
            f"modified={status.modified}; untracked={status.untracked}"
        )
        changed = gitutil.changed_files(repo, gitutil.head(repo))
        if changed:
            parts.append("Changed paths: " + ", ".join(changed[:50]))

    root_names = sorted(
        item.name
        for item in repo.iterdir()
        if item.name not in _BLOCKED_NAMES and not item.name.startswith(".env")
    )[:100]
    parts.append("Root entries: " + ", ".join(root_names))

    used = sum(len(part) for part in parts)
    for path in files:
        if used >= max_chars:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        remaining = max_chars - used
        excerpt = text[: min(per_file_chars, remaining)]
        rel = path.relative_to(repo).as_posix()
        section = f"\n### Evidence file: {rel}\n{excerpt}"
        parts.append(section)
        used += len(section)
    return "\n".join(parts).strip() + "\n"


def _patterns(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [part.strip() for part in text.replace(",", "\n").splitlines() if part.strip()]


def _matching_files(repo: Path, patterns: list[str]) -> list[Path]:
    matches: list[Path] = []
    excluded_dirs = {".git", "node_modules", ".next", ".venv", "venv", "dist", "build"}
    for root, dirs, filenames in os.walk(repo):
        dirs[:] = [name for name in dirs if name not in excluded_dirs]
        base = Path(root)
        for name in filenames:
            path = base / name
            if name in _BLOCKED_NAMES or name.startswith(".env"):
                continue
            rel = path.relative_to(repo).as_posix()
            if path.suffix.lower() not in _SAFE_SUFFIXES:
                continue
            try:
                if path.stat().st_size > 250_000:
                    continue
            except OSError:
                continue
            if any(fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern) for pattern in patterns):
                matches.append(path)
    return sorted(set(matches), key=lambda item: item.relative_to(repo).as_posix())[:25]
