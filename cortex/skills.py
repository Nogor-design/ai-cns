"""Shared skill cards: one body of guidance, rendered for each tool.

Every worker gets told the same things about how work here is judged, written
once. The content is not style advice; it is the gate's contract, stated
up front, because the real runs of 2026-09-17 showed the gate rejecting
*correct* work for reasons the agent was never told about -- build artifacts
from its own test run counted as files changed outside the task's scope.

Two deliberate constraints:

* **Cards are rendered into the brief, never written into a repository.** The
  gate protects ``CLAUDE.md``, ``AGENTS.md`` and agent configuration precisely
  because changing how later runs behave is the owner's decision. A tool that
  wrote its own instruction files would be defeating that on the way in.
* **A local model gets the compact form.** The same guidance, without the
  headings and prose that a 14B model pays for in prefill and does not need.

The cards say what Cortex actually checks. When the gate changes, these change
with it -- a card that promises something the gate does not check is worse than
no card.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import verification

# Workers without repository tools, whose prompt is their whole world and whose
# prefill cost is paid locally per token.
COMPACT_WORKERS = frozenset({"ollama", "opencode-local"})


@dataclass(frozen=True)
class Card:
    name: str
    lines: tuple[str, ...]
    compact: tuple[str, ...] = ()

    def render(self, *, compact: bool) -> list[str]:
        return list(self.compact or self.lines) if compact else list(self.lines)


GATE = Card(
    "How this change will be judged",
    (
        "A second model that did not write this change will judge it against the",
        "acceptance criteria, and Cortex will check the change mechanically before",
        "anything merges. It is checked for: files changed outside the allowed",
        "paths, changes to protected files, secrets in the diff, and the project's",
        "test command passing both on this branch and again after merging.",
        "Work that fails any of these is not merged and may be handed to a",
        "different model, so getting it right the first time is the cheap path.",
    ),
    (
        "Your change is checked automatically: allowed paths only, no protected",
        "files, no secrets, tests must pass. Anything else is rejected.",
    ),
)

ARTIFACTS = Card(
    "Do not commit what your own tools produced",
    (
        "Running tests or a build creates files. Caches, compiled bytecode,",
        "coverage data and build output are not part of your change: leave them",
        "uncommitted, or add them to the repository's ignore file if it has none.",
        "Cortex counts every file the branch changed, so a stray `__pycache__`",
        "reads as work outside your task's scope and loses an otherwise good",
        "change.",
    ),
    ("Never commit caches, bytecode or build output; they count against your scope.",),
)

SCOPE = Card(
    "Stay inside the task's scope",
    (
        "When the task lists allowed paths, they are the whole permitted surface.",
        "If the work genuinely cannot be done inside them, stop and say so in your",
        "final message rather than reaching outside: a change that exceeds its",
        "scope is rejected whether or not it is correct.",
    ),
    ("Only touch the allowed paths. If that is impossible, say so and stop.",),
)

FINISHING = Card(
    "Finishing",
    (
        "Commit your work on the branch you are on, or leave it in the working",
        "tree; either is fine, Cortex commits what you leave. Do not create",
        "branches, do not merge, do not push, and do not touch the repository",
        "outside this worktree.",
        "End with a short statement of what you changed and why it meets each",
        "acceptance criterion; that is what the reviewing model reads first.",
    ),
    (
        "Leave your work in the worktree or commit it. Never branch, merge or push.",
        "End by stating how the change meets each acceptance criterion.",
    ),
)

REVIEW = Card(
    "Reviewing, not fixing",
    (
        "You are judging someone else's change, not improving it. Do not edit any",
        "file. Read the diff against the acceptance criteria and answer on the",
        "contract you were given; a thorough read of the diff is worth more than",
        "exploring the rest of the repository.",
    ),
    ("Judge the diff only. Change nothing. Answer exactly on the given contract.",),
)

RESEARCH = Card(
    "Research",
    (
        "Answer with sources. Prefer what can be checked over what sounds right,",
        "say plainly when the evidence is thin, and do not change any file.",
    ),
    ("Cite sources, flag thin evidence, change nothing.",),
)

# Which cards apply to which kind of work. Write actions get the full contract;
# read-only actions get the parts that apply to them.
BY_ACTION: dict[str, tuple[Card, ...]] = {
    "implement": (GATE, SCOPE, ARTIFACTS, FINISHING),
    "draft": (GATE, SCOPE, ARTIFACTS, FINISHING),
    "review": (REVIEW,),
    "research": (RESEARCH,),
}


def cards_for(action: str) -> tuple[Card, ...]:
    return BY_ACTION.get(action, (SCOPE,))


def protected_note(compact: bool = False) -> str:
    """The protected files, named, from the gate's own list.

    Generated rather than written out, so a new protected pattern reaches the
    agents that must avoid it without anyone remembering to update prose.
    """
    patterns = sorted({pattern for pattern, _ in verification.PROTECTED_FILES})
    joined = ", ".join(f"`{pattern}`" for pattern in patterns)
    if compact:
        return f"Protected (never change): {', '.join(patterns)}."
    return (
        "Protected files, which an unattended change may not touch without the "
        f"owner's decision: {joined}."
    )


def render(
    action: str,
    *,
    worker: str,
    test_command: str | None = None,
    allowed_paths: str | None = None,
) -> str:
    """The skill card block for one worker on one kind of work."""
    compact = worker in COMPACT_WORKERS
    lines: list[str] = ["## How Cortex works with you" if not compact else "RULES"]
    for card in cards_for(action):
        if not compact:
            lines.append("")
            lines.append(f"### {card.name}")
        lines.extend(card.render(compact=compact))
    if action in {"implement", "draft"}:
        lines.append("")
        lines.append(protected_note(compact))
        paths = _paths(allowed_paths)
        if paths:
            lines.append(
                ("Allowed paths: " if compact else "The allowed paths for this task are: ")
                + ", ".join(f"`{path}`" for path in paths)
                + "."
            )
        if test_command:
            lines.append(
                ("Tests: " if compact else "Cortex will run this project's tests as ")
                + f"`{test_command}`"
                + ("." if compact else ", on your branch and again on the merged result.")
            )
    return "\n".join(lines).strip() + "\n"


def for_task(project: sqlite3.Row, task: sqlite3.Row, *, worker: str, action: str) -> str:
    return render(
        action,
        worker=worker,
        test_command=_field(project, "test_command"),
        allowed_paths=_field(task, "allowed_paths"),
    )


def payload() -> dict[str, Any]:
    """The cards themselves, so the owner can read what agents are told."""
    return {
        "cards": [
            {"name": card.name, "lines": list(card.lines),
             "compact": list(card.compact)}
            for card in (GATE, SCOPE, ARTIFACTS, FINISHING, REVIEW, RESEARCH)
        ],
        "by_action": {action: [card.name for card in cards]
                      for action, cards in BY_ACTION.items()},
        "compact_workers": sorted(COMPACT_WORKERS),
        "protected": protected_note(),
    }


def _paths(allowed: str | None) -> list[str]:
    if not allowed:
        return []
    try:
        parsed = json.loads(allowed)
        values = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        values = [part.strip() for part in allowed.replace(",", "\n").splitlines()]
    return [str(value).strip() for value in values if str(value).strip()]


def _field(row: sqlite3.Row, name: str) -> str | None:
    try:
        return row[name]
    except (IndexError, KeyError):
        return None
