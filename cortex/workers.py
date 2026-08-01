"""Headless CLI worker adapters.

Commands are argument arrays and execute without a shell. Full task briefs use
stdin where supported so private context is not exposed in process listings.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import ollama_client


class WorkerError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandSpec:
    worker: str
    argv: tuple[str, ...]
    uses_stdin: bool

    @property
    def display(self) -> str:
        return subprocess.list2cmdline(list(self.argv))


@dataclass(frozen=True)
class WorkerResult:
    exit_code: int
    stdout: str
    stderr: str
    usage: dict[str, object] | None


_PROBE_CACHE: dict[str, tuple[float, dict[str, str | None]]] = {}


def available(worker: str) -> bool:
    if worker == "ollama":
        return ollama_client.available()
    return worker != "perplexity" and shutil.which(_executable(worker)) is not None


def probe(worker: str, *, max_age: float = 300) -> dict[str, str | None]:
    cached = _PROBE_CACHE.get(worker)
    if cached and time.monotonic() - cached[0] < max_age:
        return dict(cached[1])
    if worker == "perplexity":
        result = {"availability": "manual", "note": "No local CLI configured"}
    elif not available(worker):
        result = {"availability": "missing", "note": "CLI command not found"}
    elif worker in {"codex", "claude", "grok"}:
        auth_command = {
            "codex": ["codex", "login", "status"],
            "claude": ["claude", "auth", "status"],
            "grok": ["grok", "models"],
        }[worker]
        try:
            proc = subprocess.run(
                auth_command, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=6,
            )
            combined = f"{proc.stdout}\n{proc.stderr}".lower()
            auth_failed = proc.returncode != 0 or any(
                marker in combined
                for marker in ("not authenticated", '"loggedin": false', "not logged in")
            )
            result = {
                "availability": "needs_auth" if auth_failed else "ready",
                "note": f"{worker.title()} CLI needs sign-in" if auth_failed else None,
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = {"availability": "unavailable", "note": str(exc)}
    else:
        result = {"availability": "ready", "note": None}
    _PROBE_CACHE[worker] = (time.monotonic(), result)
    return dict(result)


def build_command(
    *,
    worker: str,
    model: str,
    action: str,
    workspace: str | Path,
    brief_path: str | Path,
    budget: str,
    effort: str | None = None,
) -> CommandSpec:
    workspace = str(workspace)
    brief_path = str(brief_path)
    write = action == "implement"

    if worker == "codex":
        argv = ["codex", "exec"]
        if effort:
            argv.extend(["--config", f'model_reasoning_effort="{effort}"'])
        argv.extend([
            "--json", "--sandbox",
            "workspace-write" if write else "read-only", "-C", workspace,
        ])
        if model and model != "default":
            argv.extend(["--model", model])
        argv.append("-")
        return CommandSpec(worker, tuple(argv), True)

    if worker == "claude":
        argv = [
            "claude", "-p", "--output-format", "json", "--permission-mode",
            "acceptEdits" if write else "plan", "--max-turns", _max_turns(budget),
            "--no-session-persistence",
        ]
        if model and model != "default":
            argv.extend(["--model", model])
        if effort:
            argv.extend(["--effort", effort])
        return CommandSpec(worker, tuple(argv), True)

    if worker == "gemini":
        argv = [
            "gemini", "-p", "Follow the complete task brief provided on stdin.",
            "--output-format", "json", "--approval-mode",
            "auto_edit" if write else "plan", "--sandbox",
        ]
        if model and model != "default":
            argv.extend(["--model", model])
        return CommandSpec(worker, tuple(argv), True)

    if worker == "grok":
        argv = [
            "grok", "--no-auto-update", "--prompt-file", brief_path,
            "--output-format", "json", "--permission-mode",
            "acceptEdits" if write else "plan", "--cwd", workspace,
        ]
        if model and model != "default":
            argv.extend(["--model", model])
        if effort:
            argv.extend(["--reasoning-effort", effort])
        return CommandSpec(worker, tuple(argv), False)

    if worker == "ollama":
        return CommandSpec(worker, ("ollama", "run", model or "phi4:14b"), True)

    if worker == "perplexity":
        raise WorkerError(
            "Perplexity is configured as a manual/API research worker; no CLI was found."
        )
    raise WorkerError(f"unknown worker: {worker}")


def execute(
    spec: CommandSpec,
    *,
    workspace: str | Path,
    brief: str,
    timeout: int,
) -> WorkerResult:
    if spec.worker == "ollama":
        try:
            response, usage = ollama_client.generate_with_usage(
                brief, model=spec.argv[-1], timeout=float(timeout)
            )
        except ollama_client.OllamaUnavailable as exc:
            raise WorkerError(str(exc)) from exc
        return WorkerResult(exit_code=0, stdout=response, stderr="", usage=usage)
    if not available(spec.worker):
        raise WorkerError(f"worker command is not available: {_executable(spec.worker)}")
    try:
        proc = subprocess.run(
            list(spec.argv),
            cwd=str(workspace),
            input=brief if spec.uses_stdin else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerError(str(exc)) from exc
    return WorkerResult(
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        usage=_extract_usage(proc.stdout),
    )


def _extract_usage(output: str) -> dict[str, object] | None:
    objects: list[dict[str, object]] = []
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            objects.append(parsed)
    except json.JSONDecodeError:
        for line in output.splitlines():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                objects.append(parsed)
    for data in reversed(objects):
        for key in ("stats", "usage", "modelUsage"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
    return None


def _max_turns(budget: str) -> str:
    return {"local": "2", "small": "3", "medium": "6", "large": "10"}.get(
        budget, "3"
    )


def _executable(worker: str) -> str:
    return worker
