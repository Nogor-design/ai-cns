"""What a project currently is, and where its own documents say it is going.

Cortex already knows what *work* exists (tasks, runs, phases). It did not know
what the repository itself claims: the README's description, the plan or roadmap
document, the unchecked boxes, and whether any of that still matches the code.

A survey answers four questions from the repo and the database alone — no agent,
no provider, no cost:

* **Does** — what the project is, from its README.
* **Plan** — where its own plan document says it goes next.
* **Open items** — the unchecked work those documents still list.
* **Drift** — where the documents and the code disagree.

Snapshots are stored, so re-evaluating shows what changed rather than only what
is true now. The deterministic reading is the product; a local model may add a
one-paragraph synthesis on top, and never replaces the extracted facts.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import gitutil, ids

# Directories that never hold a project's own prose, and are large enough that
# walking them would dominate the scan.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", ".next", ".nuxt", "target", "vendor", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages", ".idea",
    ".vs", "bin", "obj", "coverage", ".cache", "logs",
})

# Only the repository root and these subdirectories are scanned for documents.
DOC_DIRS = ("docs", "doc", "documentation", ".cortex", "planning", "plans")
# Scanned one level deep only; see `_candidate_files`.
FLAT_DIRS = frozenset({".cortex"})

MAX_DOCUMENTS = 60
MAX_DOC_BYTES = 400_000
SUMMARY_CHARS = 420
MAX_OPEN_ITEMS = 12

# Heading text under which a bullet list is a statement of remaining work.
_NEXT_HEADING = re.compile(
    r"^#{1,6}\s*(?:\d+[.)]\s*)?("
    r"next(?!\.js)|next steps?|remaining|to ?do|todo|roadmap|upcoming|backlog|"
    r"outstanding|future|planned|what'?s next|follow[- ]ups?|open questions?"
    r")\b",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
# A prose lead-in that introduces a list of remaining work, e.g.
# "Remaining to complete end-to-end rollout:".
_NEXT_LEAD_IN = re.compile(
    r"^.{0,80}\b(?:next(?!\.js)|remaining|to ?do|todo|upcoming|backlog|outstanding|"
    r"planned|follow[- ]ups?|still (?:to|needs?))\b.{0,50}:\s*$",
    re.IGNORECASE,
)
# "## Phase 3 - Verification gates", "### Milestone 2: ...", "## Step 4".
_PHASE_HEADING = re.compile(
    r"^#{2,6}\s+(?:phase|stage|step|milestone|sprint|iteration)\s*\d+\b",
    re.IGNORECASE,
)
# A phase heading that announces its own completion.
_PHASE_DONE = re.compile(
    r"\b(done|complete|completed|shipped|delivered|merged|closed|landed)\b",
    re.IGNORECASE,
)
# Labelled prose inside a phase section: "Exit: ...", "Scope: ...".
_PHASE_LABEL = re.compile(
    r"^(scope|exit(?: criteria)?|remaining|next|deliverables?|goal|non-goals?)"
    r"\s*:\s*(.+\S)\s*$",
    re.IGNORECASE,
)
_UNCHECKED = re.compile(r"^\s*[-*+]\s*\[\s\]\s+(.*\S)\s*$")
_CHECKED = re.compile(r"^\s*[-*+]\s*\[[xX]\]\s+(.*\S)\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_STATUS_LINE = re.compile(r"^\s*(?:\*\*)?status(?:\*\*)?\s*[:：]\s*(.+\S)\s*$", re.IGNORECASE)
_BADGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# Scaffolded documents carry angle-bracket placeholders ("<task> (type, priority)").
# They are instructions to a future author, not work anybody planned.
_PLACEHOLDER = re.compile(r"^<[^>]*>")
# A lone italic metadata line ("_Last updated: ..._") is not a description.
_METADATA_LINE = re.compile(r"^_[^_].*_$")

# Filename patterns, most specific first; the first match names the kind.
_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("readme", re.compile(r"^readme(\.[a-z]+)?\.(md|markdown|rst|txt)$", re.I)),
    ("state", re.compile(r"^state\.md$", re.I)),
    ("roadmap", re.compile(r"roadmap", re.I)),
    ("plan", re.compile(r"plan|phase|milestone|design|architecture|spec", re.I)),
    ("todo", re.compile(r"^(todo|tasks?|backlog)\b", re.I)),
    ("changelog", re.compile(r"^changelog|^history|^releases?\b", re.I)),
    ("contributing", re.compile(r"^contributing|^code_of_conduct|^license", re.I)),
)

# Which document speaks for "where this is going", best first.
_PLAN_PRIORITY = ("blueprint", "plan", "roadmap", "state", "todo", "readme")


@dataclass(frozen=True)
class Document:
    """One prose file Cortex read, and what it took from it."""

    path: str
    kind: str
    title: str | None
    status: str | None
    summary: str
    open_items: tuple[str, ...] = ()
    done_items: int = 0
    current_phase: str | None = None
    phases_done: int = 0
    modified_at: str | None = None
    size: int = 0

    def to_dict(self) -> dict[str, Any]:
        item = asdict(self)
        item["open_items"] = list(self.open_items)
        return item


@dataclass(frozen=True)
class Survey:
    """One reading of a project, at one moment."""

    project_id: str
    created_at: str
    source: str
    does: str
    plan_says: str
    plan_source: str | None
    next_steps: tuple[str, ...]
    documents: tuple[Document, ...]
    signals: dict[str, Any]
    drift: tuple[str, ...]
    synthesis: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "created_at": self.created_at,
            "source": self.source,
            "does": self.does,
            "plan_says": self.plan_says,
            "plan_source": self.plan_source,
            "next_steps": list(self.next_steps),
            "documents": [doc.to_dict() for doc in self.documents],
            "signals": self.signals,
            "drift": list(self.drift),
            "synthesis": self.synthesis,
            "notes": list(self.notes),
            "fingerprint": self.fingerprint,
        }


def _fingerprint(survey: Survey) -> str:
    """Identify the *content* of a survey, ignoring when it was taken.

    Re-evaluating an untouched project must produce the same fingerprint, so the
    dashboard can say "nothing has changed" instead of showing a new snapshot
    that differs only by timestamp.
    """
    material = {
        "does": survey.does,
        "plan_says": survey.plan_says,
        "plan_source": survey.plan_source,
        "next_steps": list(survey.next_steps),
        "drift": list(survey.drift),
        "documents": [
            {
                "path": doc.path, "kind": doc.kind, "status": doc.status,
                "summary": doc.summary, "open_items": list(doc.open_items),
            }
            for doc in survey.documents
        ],
        # Volatile counters (dirty files, task counts) deliberately excluded:
        # they change constantly and would defeat the "unchanged" signal.
        "signals": {
            key: survey.signals.get(key)
            for key in ("head", "is_git", "branch")
        },
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# reading the repository


def _classify(path: Path, blueprint: Path | None) -> str:
    if blueprint is not None:
        try:
            if path.resolve() == blueprint.resolve():
                return "blueprint"
        except OSError:  # pragma: no cover - unreadable path
            pass
    name = path.name
    for kind, pattern in _KINDS:
        if pattern.search(name):
            return kind
    return "notes"


def _candidate_files(root: Path) -> Iterable[Path]:
    """Markdown and text files at the root and in the usual document folders."""
    suffixes = {".md", ".markdown", ".rst", ".txt"}
    try:
        for entry in sorted(root.iterdir()):
            if entry.is_file() and entry.suffix.lower() in suffixes:
                yield entry
    except OSError:
        return
    for folder in DOC_DIRS:
        directory = root / folder
        if not directory.is_dir():
            continue
        try:
            # `.cortex/` is Cortex's own working directory: below its top level
            # sit generated run briefs and review transcripts, which would
            # otherwise be read back as if the project had authored them.
            entries = sorted(
                directory.iterdir() if folder in FLAT_DIRS else directory.rglob("*")
            )
        except OSError:  # pragma: no cover - permission denied mid-walk
            continue
        for entry in entries:
            if entry.is_dir():
                continue
            if any(part in SKIP_DIRS for part in entry.relative_to(root).parts):
                continue
            if entry.suffix.lower() in suffixes:
                yield entry


def _clean(line: str) -> str:
    line = _BADGE.sub("", line)
    line = _LINK.sub(r"\1", line)
    line = line.replace("**", "").replace("`", "")
    return line.strip()


def _first_paragraph(lines: list[str]) -> str:
    """The first real sentence of prose, skipping title, badges and metadata."""
    collected: list[str] = []
    for raw in lines:
        line = _clean(raw)
        if not line:
            if collected:
                break
            continue
        if line.startswith("#") or line.startswith(">"):
            if collected:
                break
            continue
        if line.startswith(("---", "===", "|", "<!--")):
            continue
        if _BULLET.match(raw) and not collected:
            # A document that opens with a list still describes itself; take
            # the first item rather than nothing.
            collected.append(_BULLET.match(raw).group(1))  # type: ignore[union-attr]
            break
        if _STATUS_LINE.match(line) or _METADATA_LINE.match(line):
            continue
        collected.append(line)
        if sum(len(part) for part in collected) > SUMMARY_CHARS:
            break
    text = " ".join(collected).strip()
    if len(text) > SUMMARY_CHARS:
        cut = text.rfind(" ", 0, SUMMARY_CHARS)
        text = text[: cut if cut > 0 else SUMMARY_CHARS].rstrip(" ,;:") + "…"
    return text


def _prepare(text: str) -> list[str]:
    """Lines ready to parse: no code fences, and wrapped bullets rejoined.

    Markdown lets one list item span several lines, and every plan document here
    uses that. Parsing raw lines would truncate half the items mid-sentence.
    Fenced code is dropped outright: it is never a statement of intent.
    """
    lines: list[str] = []
    fenced = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            continue
        if fenced:
            continue
        joinable = (
            stripped
            and lines
            and not _BULLET.match(raw)
            and not _HEADING.match(raw)
        )
        if joinable:
            previous = lines[-1]
            # An indented line continues the bullet above it; an unindented one
            # continues a labelled paragraph ("Exit: …") until the blank line
            # that ends it.
            continues_label = (
                not raw[:1].isspace()
                and _PHASE_LABEL.match(_clean(previous))
                and not _PHASE_LABEL.match(stripped)
            )
            if (raw[:1].isspace() and _BULLET.match(previous)) or continues_label:
                lines[-1] = f"{previous.rstrip()} {stripped}"
                continue
        lines.append(raw)
    return lines


def _current_phase(lines: list[str]) -> tuple[str | None, list[str], int]:
    """The first phase heading that does not announce itself as finished.

    Phased plans are the common shape here, and the single most useful answer to
    "where does this go next" is the earliest unfinished phase plus whatever
    bullets sit under it. Completion is read from the heading's own words, so a
    plan that never marks anything done yields its first phase, which is correct.
    """
    current: str | None = None
    items: list[str] = []
    finished = 0
    depth = 0
    capturing = False
    for raw in lines:
        heading = _HEADING.match(raw)
        if heading:
            level = len(heading.group(1))
            text = _clean(heading.group(2))
            if capturing and level <= depth:
                capturing = False
            if _PHASE_HEADING.match(raw):
                if _PHASE_DONE.search(text):
                    finished += 1
                    continue
                if current is None:
                    current, depth, capturing = text, level, True
            continue
        if not capturing or len(items) >= MAX_OPEN_ITEMS:
            continue
        bullet = _UNCHECKED.match(raw) or _BULLET.match(raw)
        if bullet:
            items.append(_clean(bullet.group(1)))
            continue
        # Some phases are written as labelled prose rather than lists, and the
        # labelled sentences ("Exit: ...", "Scope: ...") are the operative ones.
        labelled = _PHASE_LABEL.match(_clean(raw))
        if labelled:
            label = labelled.group(1).strip().rstrip(":").title()
            items.append(f"{label}: {labelled.group(2).strip()}")
    return current, items, finished


def _open_items(lines: list[str]) -> tuple[list[str], int]:
    """Remaining work stated in a document.

    Three shapes, because real plan documents use all three: unchecked boxes
    anywhere; bullets under a "Next steps" heading; and bullets under a plain
    lead-in line like "Remaining to complete rollout:". The lead-in form is
    common in prose-style plans that never adopted checkboxes.
    """
    items: list[str] = []
    done = 0
    under_next = False
    for raw in lines:
        heading = _HEADING.match(raw)
        if heading:
            under_next = bool(_NEXT_HEADING.match(raw))
            continue
        if _CHECKED.match(raw):
            done += 1
            continue
        unchecked = _UNCHECKED.match(raw)
        if unchecked:
            items.append(_clean(unchecked.group(1)))
            continue
        bullet = _BULLET.match(raw)
        if bullet:
            if under_next:
                items.append(_clean(bullet.group(1)))
            continue
        line = _clean(raw)
        if not line:
            continue
        # A sentence ending in a colon introduces the list that follows it; any
        # other prose closes whatever list was open.
        under_next = bool(_NEXT_LEAD_IN.match(line))
    seen: set[str] = set()
    unique = []
    for item in items:
        key = item.lower()
        if item and key not in seen and not _PLACEHOLDER.match(item):
            seen.add(key)
            unique.append(item)
    return unique, done


def read_document(path: Path, root: Path, kind: str) -> Document | None:
    try:
        stat = path.stat()
        if stat.st_size > MAX_DOC_BYTES:
            text = path.read_text(encoding="utf-8", errors="replace")[:MAX_DOC_BYTES]
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = _HTML_COMMENT.sub("", text)
    lines = _prepare(text)
    title = None
    status = None
    for raw in lines[:40]:
        heading = _HEADING.match(raw)
        if heading and title is None and len(heading.group(1)) <= 2:
            title = _clean(heading.group(2))
        matched = _STATUS_LINE.match(_clean(raw))
        if matched and status is None:
            status = matched.group(1).strip()
    items, done = _open_items(lines)
    phase, phase_items, phases_done = _current_phase(lines)
    if phase:
        # A phased plan's remaining work is the unfinished phase, so it leads;
        # lead-in lists elsewhere in the document are older context behind it.
        items = phase_items + [item for item in items if item not in phase_items]
    try:
        relative = str(path.relative_to(root)).replace("\\", "/")
    except ValueError:  # pragma: no cover - path outside the repo
        relative = path.name
    return Document(
        path=relative,
        kind=kind,
        title=title,
        status=status,
        summary=_first_paragraph(lines),
        open_items=tuple(items[:MAX_OPEN_ITEMS]),
        done_items=done,
        current_phase=phase,
        phases_done=phases_done,
        modified_at=_iso(stat.st_mtime),
        size=stat.st_size,
    )


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_documents(root: Path, blueprint: Path | None = None) -> list[Document]:
    documents: list[Document] = []
    for path in _candidate_files(root):
        if len(documents) >= MAX_DOCUMENTS:
            break
        kind = _classify(path, blueprint)
        if kind == "contributing":
            continue  # boilerplate; says nothing about this project
        document = read_document(path, root, kind)
        # A document whose only content is a Status: line still speaks for the
        # project; one with nothing at all is skipped.
        if document is not None and (
            document.summary or document.open_items or document.status
        ):
            documents.append(document)
    order = {kind: index for index, kind in enumerate(_PLAN_PRIORITY)}
    documents.sort(key=lambda doc: (order.get(doc.kind, len(order)), doc.path))
    return documents


# --------------------------------------------------------------------------
# reading the database and git


def _repo_signals(root: Path) -> dict[str, Any]:
    signals: dict[str, Any] = {"repo_path": str(root), "exists": root.is_dir()}
    if not signals["exists"]:
        return signals
    snapshot = gitutil.dashboard_snapshot(root, timeout=6)
    signals["is_git"] = snapshot.is_git
    signals["branch"] = snapshot.branch
    signals["dirty"] = snapshot.summary.dirty if snapshot.summary.available else None
    if snapshot.last_commit:
        when, sha, subject = snapshot.last_commit
        signals["head"] = sha
        signals["last_commit_at"] = when
        signals["last_commit_subject"] = subject
        signals["days_since_commit"] = _days_since(when)
    return signals


def _days_since(stamp: str | None) -> int | None:
    if not stamp:
        return None
    text = str(stamp).strip().replace("Z", "+00:00")
    for parser in (datetime.fromisoformat,):
        try:
            moment = parser(text)
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - moment).days)
    return None


def _work_signals(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY status",
        (project_id,),
    ).fetchall()
    counts = {str(row["status"]): int(row["n"]) for row in rows}
    last_run = conn.execute(
        """SELECT started_at, outcome, model FROM runs
           WHERE project_id = ? ORDER BY started_at DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    signals: dict[str, Any] = {
        "tasks": counts,
        "open_tasks": sum(
            count for status, count in counts.items()
            if status not in {"done", "cancelled", "archived"}
        ),
    }
    if last_run:
        signals["last_run_at"] = last_run["started_at"]
        signals["last_run_outcome"] = last_run["outcome"]
        signals["last_run_model"] = last_run["model"]
        signals["days_since_run"] = _days_since(last_run["started_at"])
    return signals


# --------------------------------------------------------------------------
# the survey itself


def _pick_plan(documents: list[Document]) -> Document | None:
    """The document that speaks for the project's direction.

    Kind decides first — an approved blueprint outranks a roadmap outranks a
    README. Within one kind the most recently edited file wins, because a repo
    with an old spec and a live plan means the live plan.
    """
    for kind in _PLAN_PRIORITY:
        candidates = [
            document for document in documents
            if document.kind == kind
            and (document.open_items or document.summary or document.status)
        ]
        if candidates:
            return max(candidates, key=lambda doc: (doc.modified_at or "", doc.path))
    return None


def _drift(
    project: sqlite3.Row,
    documents: list[Document],
    plan: Document | None,
    signals: dict[str, Any],
) -> list[str]:
    """Where the documents and the repository disagree. Observations, not blame."""
    notes: list[str] = []
    if not any(doc.kind == "readme" for doc in documents):
        notes.append("No README: nothing in the repo says what this project is.")
    if plan is None:
        notes.append(
            "No plan, roadmap or state document: the repo does not say where it is going."
        )
    else:
        plan_age = _days_since(plan.modified_at)
        commit_age = signals.get("days_since_commit")
        if plan_age is not None and commit_age is not None and plan_age - commit_age >= 14:
            notes.append(
                f"{plan.path} was last touched {plan_age} days ago but the code moved "
                f"{commit_age} days ago — the plan may be behind the work."
            )
    open_items = sum(len(doc.open_items) for doc in documents)
    open_tasks = signals.get("open_tasks") or 0
    if open_items and not open_tasks:
        notes.append(
            f"{open_items} unchecked item(s) in the documents, but no open Cortex "
            "task — nothing here is scheduled."
        )
    commit_age = signals.get("days_since_commit")
    if commit_age is not None and commit_age >= 30:
        notes.append(f"No commit for {commit_age} days.")
    status = project["blueprint_status"] if "blueprint_status" in project.keys() else None
    if status in {"missing", None}:
        notes.append("No approved Cortex blueprint, so phases cannot drive work here.")
    elif status == "stale":
        notes.append("The approved blueprint is stale: the file changed outside Cortex.")
    return notes


def build(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    *,
    synthesize: bool = False,
) -> Survey:
    """Read one project and describe it. Never writes, never dispatches."""
    root = Path(str(project["repo_path"]))
    blueprint = None
    blueprint_path = (
        project["blueprint_path"] if "blueprint_path" in project.keys() else None
    )
    if blueprint_path:
        candidate = Path(str(blueprint_path))
        blueprint = candidate if candidate.is_absolute() else root / candidate
    notes: list[str] = []
    documents = read_documents(root, blueprint) if root.is_dir() else []
    if not root.is_dir():
        notes.append(f"Repository path does not exist: {root}")

    signals = _repo_signals(root)
    signals.update(_work_signals(conn, str(project["id"])))

    readme = next((doc for doc in documents if doc.kind == "readme"), None)
    goal = str(project["current_goal"] or "").strip()
    does = (readme.summary if readme and readme.summary else "") or goal
    if not does:
        does = "Not stated: no README summary and no goal recorded in Cortex."

    plan = _pick_plan(documents)
    plan_says = ""
    if plan is not None:
        # An unfinished phase heading is the most direct answer; a Status: line
        # is the next best; the opening paragraph is the fallback.
        if plan.current_phase:
            plan_says = f"Current phase: {plan.current_phase}"
        else:
            plan_says = plan.status or plan.summary
    if not plan_says:
        plan_says = goal or "Not stated in any document Cortex could find."

    # Only the plan document's own items are "where this goes next". Borrowing
    # from every other markdown file resurrects work that was abandoned years
    # ago and presents it as current, which is worse than showing nothing.
    next_steps: list[str] = list(plan.open_items) if plan is not None else []
    if not next_steps:
        for document in documents:
            if document.kind in {"state", "changelog"} or document is plan:
                continue
            for item in document.open_items:
                if item not in next_steps:
                    next_steps.append(item)
            if next_steps:
                break

    survey = Survey(
        project_id=str(project["id"]),
        created_at=ids.now(),
        source="local",
        does=does,
        plan_says=plan_says,
        plan_source=plan.path if plan else None,
        next_steps=tuple(next_steps[:MAX_OPEN_ITEMS]),
        documents=tuple(documents),
        signals=signals,
        drift=tuple(_drift(project, documents, plan, signals)),
        notes=tuple(notes),
    )
    if synthesize:
        survey = _synthesize(survey, project)
    return survey


_SYNTHESIS_PROMPT = """You are summarising a software project for its owner.
Use ONLY the facts below. Do not invent features, dates or status.
Write at most four sentences: what it is, where its plan says it goes next,
and the single most important thing that looks out of date. Plain prose, no
headings, no bullet points, no preamble.

PROJECT: {name}
WHAT THE README SAYS: {does}
WHAT THE PLAN SAYS ({plan_source}): {plan_says}
UNCHECKED ITEMS: {next_steps}
OBSERVED MISMATCHES: {drift}
"""


def _synthesize(survey: Survey, project: sqlite3.Row) -> Survey:
    """Add a local-model paragraph. Failure is silent: the facts already stand."""
    from . import ollama_client

    prompt = _SYNTHESIS_PROMPT.format(
        name=project["name"],
        does=survey.does,
        plan_source=survey.plan_source or "no plan document",
        plan_says=survey.plan_says,
        next_steps="; ".join(survey.next_steps) or "none found",
        drift="; ".join(survey.drift) or "none",
    )
    try:
        text = ollama_client.generate(prompt).strip()
    except Exception:  # noqa: BLE001 - a summary is a bonus, never a failure
        return survey
    if not text:
        return survey
    return Survey(
        **{**{key: getattr(survey, key) for key in (
            "project_id", "created_at", "does", "plan_says", "plan_source",
            "next_steps", "documents", "signals", "drift", "notes",
        )},
            "source": "local+ollama",
            "synthesis": text,
        }
    )


# --------------------------------------------------------------------------
# snapshots


def record(conn: sqlite3.Connection, survey: Survey) -> dict[str, Any]:
    """Store a survey unless an identical one is already the latest.

    Returns the stored (or reused) row plus ``changed``, so the dashboard can
    say "re-checked, nothing moved" rather than stacking identical snapshots.
    """
    previous = latest(conn, survey.project_id)
    if previous and previous["fingerprint"] == survey.fingerprint:
        conn.execute(
            "UPDATE project_surveys SET checked_at = ? WHERE id = ?",
            (survey.created_at, previous["id"]),
        )
        conn.commit()
        reused = dict(previous)
        reused["checked_at"] = survey.created_at
        return {"survey": reused, "changed": False, "previous": previous}
    survey_id = ids.short_id()
    conn.execute(
        """INSERT INTO project_surveys
           (id, project_id, created_at, checked_at, source, fingerprint, digest_json)
           VALUES (?,?,?,?,?,?,?)""",
        (
            survey_id, survey.project_id, survey.created_at, survey.created_at,
            survey.source, survey.fingerprint,
            json.dumps(survey.to_dict(), ensure_ascii=False, default=str),
        ),
    )
    conn.commit()
    stored = conn.execute(
        "SELECT * FROM project_surveys WHERE id = ?", (survey_id,)
    ).fetchone()
    return {"survey": _row(stored), "changed": True, "previous": previous}


def latest(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT * FROM project_surveys WHERE project_id = ?
           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    return _row(row) if row else None


def history(
    conn: sqlite3.Connection, project_id: str, limit: int = 10
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM project_surveys WHERE project_id = ?
           ORDER BY created_at DESC, rowid DESC LIMIT ?""",
        (project_id, limit),
    ).fetchall()
    return [_row(row) for row in rows]


def _row(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in row.keys()}
    try:
        item["digest"] = json.loads(item.pop("digest_json"))
    except (json.JSONDecodeError, KeyError, TypeError):
        item["digest"] = {}
    return item


def refresh(
    conn: sqlite3.Connection, project: sqlite3.Row, *, synthesize: bool = False
) -> dict[str, Any]:
    """Re-read a project and store the result. This is the re-evaluate action."""
    return record(conn, build(conn, project, synthesize=synthesize))


def payload(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """What the dashboard shows for one project without re-reading the disk."""
    current = latest(conn, project_id)
    return {
        "project_id": project_id,
        "survey": current,
        "history": history(conn, project_id, limit=5),
    }
