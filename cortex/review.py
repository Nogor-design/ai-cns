"""The second-model review: a different model judges the work before it merges.

The model that wrote a change is the worst judge of whether it meets the
acceptance criteria, so Phase 3's gate ends with a reviewer that is never the
producing worker. The reviewer runs read-only against the diff and answers on a
fixed contract:

    VERDICT: pass | fail
    REASON: <one line>

Anything else — no verdict, a crash, no eligible reviewer — is a **fail**. A
review that cannot be read is not a review, and silently merging on an
ambiguous answer is exactly the failure mode the gate exists to prevent. The
owner can turn the requirement off per portfolio (``verification.review``), and
that is a deliberate, recorded choice rather than a default.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import capacity, config, lanes, policy, routing, settings, workers

REVIEWER_KEY = "verification.reviewer"
REQUIRED_KEY = "verification.review"
DEFAULT_TIMEOUT = 900

# Reviewers in order of preference: strong general reasoners first, then the
# cheap and local options, so a portfolio with one CLI installed still has one.
PREFERENCE: tuple[str, ...] = (
    "codex", "claude", "gemini", "grok", "agy", "opencode", "opencode-local", "ollama",
)

_VERDICT = re.compile(r"^\s*VERDICT\s*[:=]\s*(pass|fail)\b", re.I | re.M)
_REASON = re.compile(r"^\s*REASON\s*[:=]\s*(.+)$", re.I | re.M)


@dataclass(frozen=True)
class ReviewOutcome:
    status: str                 # pass | fail | skipped
    reviewer: str | None = None
    model: str | None = None
    reasons: tuple[str, ...] = ()
    text: str | None = None
    usage: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"pass", "skipped"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "reviewer": self.reviewer, "model": self.model,
            "reasons": list(self.reasons), "usage": self.usage,
            "excerpt": (self.text or "")[-2000:] or None,
        }


def is_required(conn: sqlite3.Connection) -> bool:
    return settings.get_bool(conn, REQUIRED_KEY, True)


def set_required(conn: sqlite3.Connection, required: bool) -> None:
    settings.set_value(conn, REQUIRED_KEY, "1" if required else "0")


def choose_reviewer(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    *,
    exclude: str,
    preferred: str | None = None,
    unattended: bool = True,
) -> tuple[str, str] | None:
    """Pick an eligible reviewer worker and model, or None if there is none.

    Eligible means: not the worker that produced the change, on this project's
    allowlist, installed and ready, and — when unattended — inside its quota or
    local lane. Those are the same rules dispatch uses, asked here so the gate
    fails with a reason instead of at the last moment.
    """
    ordered = [
        name for name in (
            preferred or settings.get(conn, REVIEWER_KEY),
            *PREFERENCE,
        ) if name
    ]
    seen: set[str] = set()
    for worker in ordered:
        worker = worker.strip().lower()
        if worker in seen or worker == exclude or worker == "perplexity":
            continue
        seen.add(worker)
        if worker not in policy.ALL_WORKERS or not policy.is_allowed(project, worker):
            continue
        # Blocking probe on purpose. The cached probe is for request paths
        # that poll; the gate runs once in a fresh process, where that cache is
        # always empty and every worker therefore reads as "checking". A real
        # run failed exactly that way, with eight checks passed and no reviewer
        # found on a machine where every CLI was installed and ready.
        if workers.probe(worker)["availability"] != "ready":
            continue
        model = routing.DEFAULT_MODELS.get(worker, "default")
        if unattended and not _admitted(conn, worker, model):
            continue
        return worker, model
    return None


def _admitted(conn: sqlite3.Connection, worker: str, model: str) -> bool:
    if worker in capacity.LOCAL:
        allowed, _ = lanes.admit(conn, model)
        return allowed
    return capacity.admit(conn, worker).allowed


def _field(task: sqlite3.Row, name: str) -> str:
    """Read a task column that may not exist on an older row."""
    try:
        return str(task[name] or "")
    except (IndexError, KeyError):
        return ""


def build_prompt(
    task: sqlite3.Row,
    *,
    diff: str,
    changed_files: list[str],
    producer: str,
) -> str:
    """A self-contained review brief: the criteria, the change, the contract."""
    acceptance = _field(task, "acceptance").strip() or (
        "No explicit acceptance criteria were recorded. Judge the change against "
        "the task description alone, and fail it if you cannot tell what it was "
        "supposed to do."
    )
    return "\n".join([
        "You are reviewing another AI agent's change before it merges into an",
        "integration branch. You did not write it and you are not fixing it.",
        "Do not modify any file; read and judge only.",
        "",
        f"## Task: {_field(task, 'title')}",
        _field(task, "brief").strip() or "(no brief recorded)",
        "",
        "## Acceptance criteria",
        acceptance,
        "",
        f"## Produced by: {producer}",
        f"## Files changed ({len(changed_files)})",
        "\n".join(f"- {path}" for path in changed_files[:100]) or "(none)",
        "",
        "## Diff",
        "```diff",
        diff or "(empty diff)",
        "```",
        "",
        "## Answer exactly in this form, and nothing after it",
        "VERDICT: pass   (only if the change meets every acceptance criterion,",
        "                 changes nothing it should not, and is safe to merge)",
        "VERDICT: fail   (otherwise)",
        "REASON: one line saying why, naming the criterion or file at issue.",
    ])


def parse(text: str) -> tuple[str | None, list[str]]:
    """Read the verdict and reasons out of a reviewer's output."""
    matches = _VERDICT.findall(text or "")
    verdict = matches[-1].lower() if matches else None
    reasons = [line.strip() for line in _REASON.findall(text or "") if line.strip()]
    return verdict, reasons[-3:]


def run(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    task: sqlite3.Row,
    *,
    diff: str,
    changed_files: list[str],
    producer: str,
    workspace: str | Path,
    unattended: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    execute_fn: Any = None,
) -> ReviewOutcome:
    """Run the second-model review and return its verdict."""
    if not is_required(conn):
        return ReviewOutcome("skipped", reasons=("Second-model review is turned off.",))
    if not diff.strip():
        return ReviewOutcome("fail", reasons=("There is no diff to review.",))

    chosen = choose_reviewer(conn, project, exclude=producer, unattended=unattended)
    if chosen is None:
        return ReviewOutcome(
            "fail",
            reasons=(
                f"No second model is available to review {producer}'s work "
                f"(allowed here: {', '.join(policy.allowed_workers(project))}).",
            ),
        )
    reviewer, model = chosen
    prompt = build_prompt(task, diff=diff, changed_files=changed_files, producer=producer)
    prompt_path = config.run_root() / task["id"] / "review.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    try:
        command = workers.build_command(
            worker=reviewer, model=model, action="review", workspace=workspace,
            brief_path=prompt_path, budget="small",
        )
        execute = execute_fn or workers.execute
        result = execute(command, workspace=workspace, brief=prompt, timeout=timeout)
    except workers.WorkerError as exc:
        return ReviewOutcome("fail", reviewer, model, (f"The reviewer could not run: {exc}",))

    text = workers.extract_text(result.stdout) or result.stdout
    verdict, reasons = parse(text)
    if result.exit_code != 0:
        return ReviewOutcome(
            "fail", reviewer, model,
            (f"{reviewer} exited with code {result.exit_code}.", *reasons),
            text, result.usage,
        )
    if verdict is None:
        return ReviewOutcome(
            "fail", reviewer, model,
            (f"{reviewer} returned no VERDICT line, so the change is not approved.",),
            text, result.usage,
        )
    return ReviewOutcome(
        verdict, reviewer, model,
        tuple(reasons) or ((f"{reviewer} approved the change.",) if verdict == "pass" else ()),
        text, result.usage,
    )
