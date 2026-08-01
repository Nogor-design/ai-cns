"""The context compiler — feature #1 (spec section 6).

Turns canonical project state into a tight, model-ready brief. Usable on day
one with no run history. Runs a local secret/privacy scan over everything about
to be emitted and blocks on any finding.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import config, secrets_scan, state, store
from .secrets_scan import Finding


class SecretsDetected(Exception):
    """Raised when the privacy scan blocks emission of a brief."""

    def __init__(self, findings: list[Finding]):
        self.findings = findings
        kinds = ", ".join(sorted({f.kind for f in findings}))
        super().__init__(f"secret/privacy scan blocked emission: {kinds}")


@dataclass
class CompiledBrief:
    text: str
    mode: str
    model: str | None
    task_id: str | None = None
    findings: list[Finding] = field(default_factory=list)


def _tailor_header(mode: str, model: str | None) -> str:
    target = model or "any model"
    if mode == "api":
        return f"<!-- brief for {target} via API; terse, self-contained -->"
    if mode == "agentic_cli":
        return (
            f"<!-- task brief for {target} (agentic CLI); paths are repo-relative; "
            f"run this agent inside the repo -->"
        )
    return f"<!-- self-contained brief for {target} (manual paste) -->"


def compile_brief(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    *,
    task: sqlite3.Row | None = None,
    model: str | None = None,
    mode: str = "manual",
    use_ollama: bool = True,
) -> CompiledBrief:
    """Compose a brief; raise SecretsDetected if the privacy scan finds anything."""
    parts: list[str] = [_tailor_header(mode, model)]

    if task is not None:
        parts.append(f"# Task: {task['title']}")
        parts.append(f"_Type: {task['type']}_")
        if task["brief"]:
            parts.append(task["brief"])
        controls: list[str] = []
        if task["risk"] and task["risk"] != "auto":
            controls.append(f"- Risk: {task['risk']}")
        if task["acceptance"]:
            controls.append(f"- Acceptance: {task['acceptance']}")
        if task["allowed_paths"]:
            controls.append(f"- Allowed paths: {task['allowed_paths']}")
        if task["budget"]:
            controls.append(f"- Budget: {task['budget']}")
        if controls:
            parts.append("## Task controls")
            parts.extend(controls)
        parts.append("")

    state_md = state.read_state(project["repo_path"])
    if state_md is None:
        # No state.md on disk yet: synthesize a scaffold from the DB row.
        state_md = state.scaffold(project)
    parts.append(state_md.rstrip())

    if mode == "agentic_cli":
        parts.append("")
        parts.append("## Definition of done reminder")
        if project["test_command"]:
            parts.append(f"- Run `{project['test_command']}` and ensure it passes.")
        parts.append("- Do not modify files outside the allowed paths when listed.")
        parts.append("- Keep the diff focused; no secrets; client-safe data only.")

    text = "\n".join(parts).strip() + "\n"

    findings = secrets_scan.scan(text, use_ollama=use_ollama)
    if findings:
        raise SecretsDetected(findings)

    if mode == "agentic_cli":
        _write_last_brief(project["repo_path"], text)

    return CompiledBrief(
        text=text,
        mode=mode,
        model=model,
        task_id=task["id"] if task is not None else None,
        findings=findings,
    )


def _write_last_brief(repo_path: str | Path, text: str) -> Path:
    path = config.last_brief_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
