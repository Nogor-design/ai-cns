"""Whether something already exists before it gets built again.

The portfolio has grown past the point where the owner can hold all of it in
mind. The risk is not forgetting a project outright; it is starting a new one
that quietly re-implements two thirds of an old one. So Cortex keeps a single
term profile per known capability and compares against it.

Two corpora, both read-only:

* every registered Cortex project — name, goal, stack, and whatever the survey
  read from its README and plan;
* the Trading Capability Hub registry, which is the authoritative catalogue of
  the local trading tools and is loaded by file path, never imported.

Scoring is plain TF-IDF cosine over the portfolio itself, so a word is only
distinctive if it is rare *here*. "dashboard" is near-worthless in this
portfolio; "ninjatrader" is decisive. No model, no network, no cost.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import survey as survey_mod

HUB_ROOT_ENV = "CORTEX_TRADING_HUB_ROOT"
DEFAULT_HUB_ROOT = Path(r"D:\trading-capability-hub")
HUB_REGISTRY = Path("registry") / "capabilities.json"

# Words that carry no signal about what a project *is*. Deliberately short: an
# aggressive list would strip the domain words that make matches meaningful.
STOPWORDS = frozenset("""
a an the and or but if then else of for to from in on at by with without into
is are was were be been being do does did doing have has had having
this that these those it its as not no nor so than too very can will just
i you he she they we me him her them us our your their my mine yours
what which who whom when where why how all any both each few more most other
some such only own same s t don now
project projects repo repository app apps application applications tool tools
system systems code codebase local new current using used use uses make makes
build builds built run runs running work works working thing things stuff
one two three first second next last set sets via per etc via also
""".split())

# A term appearing in almost every project says nothing; one appearing in two
# says a lot. Below this many characters a token is noise.
MIN_TERM_LENGTH = 3
# Fields are not equally telling. A name is a deliberate label; a plan is the
# author's own summary; a README paragraph is descriptive but diffuse.
FIELD_WEIGHTS = {"name": 4.0, "goal": 2.5, "plan": 2.0, "stack": 2.0, "text": 1.0}

_TOKEN = re.compile(r"[a-z][a-z0-9+#.]*", re.IGNORECASE)


@dataclass(frozen=True)
class Entry:
    """One thing that already exists, reduced to weighted terms."""

    key: str
    label: str
    source: str          # "project" | "hub"
    detail: str
    terms: Counter[str]
    surfaces: dict[str, str]
    project_id: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class Match:
    """Two things that look like the same idea."""

    key: str
    label: str
    source: str
    score: float
    shared: tuple[str, ...]
    detail: str
    project_id: str | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "source": self.source,
            "score": round(self.score, 4), "shared": list(self.shared),
            "detail": self.detail, "project_id": self.project_id, "path": self.path,
        }


def stem(word: str) -> str:
    """Fold the endings that stop obvious matches from matching.

    Without this, "a tool to track decisions my team makes" scores zero against
    a project described as "a decision register for small teams" — the words are
    the same idea and different strings. This is deliberately crude: it only
    handles the regular English endings that cost real recall, and it never
    shortens a word below four characters, where collisions start to hurt.
    """
    for suffix, replacement in (
        ("ies", "y"), ("ing", ""), ("ers", "er"), ("ed", ""), ("es", ""), ("s", ""),
    ):
        if word.endswith(suffix) and len(word) - len(suffix) + len(replacement) >= 4:
            if suffix == "s" and word.endswith(("ss", "us", "is")):
                continue
            return word[: len(word) - len(suffix)] + replacement
    return word


def tokenize(text: str | None) -> list[str]:
    if not text:
        return []
    words = [word.lower().strip(".") for word in _TOKEN.findall(str(text))]
    return [
        stem(word) for word in words
        if len(word) >= MIN_TERM_LENGTH and word not in STOPWORDS
    ]


def _weigh(fields: dict[str, str | None]) -> tuple[Counter[str], dict[str, str]]:
    """Term counts with field weighting folded in, plus adjacent-word pairs.

    Bigrams matter here: "market data" and "decision register" identify a
    project in a way their separate words do not.

    The second return value maps each stem back to a word someone actually
    wrote, so the UI can say the projects share "trading" rather than "trad".
    """
    terms: Counter[str] = Counter()
    surfaces: dict[str, str] = {}
    for field, value in fields.items():
        weight = FIELD_WEIGHTS.get(field, 1.0)
        words = tokenize(value)
        originals = _originals(value)
        for index, word in enumerate(words):
            terms[word] += weight
            written = originals[index] if index < len(originals) else word
            # Prefer the longest spelling seen: "trading" over "trade".
            if len(written) > len(surfaces.get(word, "")):
                surfaces[word] = written
        for first, second in zip(words, words[1:]):
            terms[f"{first} {second}"] += weight * 0.75
    for phrase in [term for term in terms if " " in term]:
        left, right = phrase.split(" ", 1)
        surfaces.setdefault(phrase, f"{surfaces.get(left, left)} {surfaces.get(right, right)}")
    return terms, surfaces


def _originals(text: str | None) -> list[str]:
    """The words tokenize kept, in their written form and the same order."""
    if not text:
        return []
    return [
        word.lower().strip(".")
        for word in _TOKEN.findall(str(text))
        if len(word) >= MIN_TERM_LENGTH and word.lower().strip(".") not in STOPWORDS
    ]


# --------------------------------------------------------------------------
# building the corpus


def _project_entries(conn: sqlite3.Connection) -> list[Entry]:
    entries: list[Entry] = []
    rows = conn.execute(
        "SELECT id, name, current_goal, stack, repo_path, status FROM projects "
        "WHERE status != 'archived'"
    ).fetchall()
    for row in rows:
        stored = survey_mod.latest(conn, str(row["id"]))
        digest = (stored or {}).get("digest") or {}
        does = str(digest.get("does") or "")
        plan = str(digest.get("plan_says") or "")
        terms, surfaces = _weigh({
            "name": str(row["name"]),
            "goal": str(row["current_goal"] or ""),
            "stack": str(row["stack"] or ""),
            "plan": plan,
            "text": does,
        })
        entries.append(Entry(
            key=f"project:{row['id']}",
            label=str(row["name"]),
            source="project",
            detail=does or str(row["current_goal"] or ""),
            terms=terms,
            surfaces=surfaces,
            project_id=str(row["id"]),
            path=str(row["repo_path"]),
        ))
    return entries


def hub_root() -> Path:
    override = os.environ.get(HUB_ROOT_ENV)
    return Path(override) if override else DEFAULT_HUB_ROOT


def _hub_entries() -> list[Entry]:
    """The Trading Capability Hub's registry, read as data.

    A missing or malformed registry is not an error: the Hub is another repo
    with its own lifecycle, and overlap detection degrades to the Cortex
    projects alone rather than failing.
    """
    path = hub_root() / HUB_REGISTRY
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        capabilities = document["capabilities"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    entries: list[Entry] = []
    for item in capabilities:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        description = str(item.get("description") or "")
        terms, surfaces = _weigh({
            "name": str(item.get("name") or ""),
            "goal": str(item.get("category") or ""),
            "text": description,
        })
        entries.append(Entry(
            key=f"hub:{item['id']}",
            label=str(item.get("name") or item["id"]),
            source="hub",
            detail=description[:300],
            terms=terms,
            surfaces=surfaces,
            path=str(item.get("owner_path") or ""),
        ))
    return entries


class Corpus:
    """Every known capability, with the IDF weights of this portfolio."""

    def __init__(self, entries: list[Entry]):
        self.entries = entries
        self.idf = self._idf(entries)
        self.surfaces: dict[str, str] = {}
        for entry in entries:
            for stemmed, written in entry.surfaces.items():
                if len(written) > len(self.surfaces.get(stemmed, "")):
                    self.surfaces[stemmed] = written

    def written(self, term: str) -> str:
        """A stem as somebody actually wrote it, for display."""
        return self.surfaces.get(term, term)

    @classmethod
    def build(cls, conn: sqlite3.Connection, *, include_hub: bool = True) -> "Corpus":
        entries = _project_entries(conn)
        if include_hub:
            entries.extend(_hub_entries())
        return cls(entries)

    @staticmethod
    def _idf(entries: list[Entry]) -> dict[str, float]:
        total = len(entries) or 1
        seen: Counter[str] = Counter()
        for entry in entries:
            seen.update(set(entry.terms))
        # Smoothed IDF: a term in every entry scores ~0, a term in one scores high.
        return {
            term: math.log((total + 1) / (count + 1)) + 1.0
            for term, count in seen.items()
        }

    def _vector(self, terms: Counter[str]) -> dict[str, float]:
        # Unseen terms get the weight of a term appearing exactly once, so a
        # query about something genuinely new is not silently zeroed out.
        default = math.log((len(self.entries) + 1) / 2) + 1.0
        return {
            term: count * self.idf.get(term, default)
            for term, count in terms.items()
        }

    def _score(self, left: dict[str, float], right: dict[str, float]) -> tuple[float, list[str]]:
        shared = set(left) & set(right)
        if not shared:
            return 0.0, []
        dot = sum(left[term] * right[term] for term in shared)
        norm = math.sqrt(sum(v * v for v in left.values())) * math.sqrt(
            sum(v * v for v in right.values())
        )
        if not norm:
            return 0.0, []
        ranked = sorted(
            shared, key=lambda term: left[term] * right[term], reverse=True
        )
        return dot / norm, ranked

    def matches_for(
        self,
        terms: Counter[str],
        *,
        exclude: str | None = None,
        limit: int = 5,
        min_score: float = 0.0,
        min_shared: int = 1,
    ) -> list[Match]:
        query = self._vector(terms)
        found: list[Match] = []
        for entry in self.entries:
            if entry.key == exclude:
                continue
            score, shared = self._score(query, self._vector(entry.terms))
            if score < min_score or len(shared) < min_shared:
                continue
            found.append(Match(
                key=entry.key, label=entry.label, source=entry.source,
                score=score, shared=tuple(self.written(term) for term in shared[:6]),
                detail=entry.detail,
                project_id=entry.project_id, path=entry.path,
            ))
        found.sort(key=lambda match: match.score, reverse=True)
        return found[:limit]


# --------------------------------------------------------------------------
# the two questions worth asking

# How close two things must be before Cortex says anything.
#
# Calibrated against the real portfolio rather than guessed. Cosine over TF-IDF
# is scale-free but not intuitive: unrelated projects land under 0.05, a shared
# domain around 0.09-0.11, and a restatement of an existing project above 0.2.
#
# Score alone is not enough. The highest-scoring pair in the portfolio was
# seagate-demo against "Imported trading-agent demos" at 0.118, on the single
# shared word "demos" — a coincidence of naming, not of purpose. Every genuine
# match shared at least two terms. So a match must clear both bars: one shared
# word is a coincidence, two is a subject.
DEFAULT_MIN_SCORE = 0.085
MIN_SHARED_TERMS = 2


def search(
    conn: sqlite3.Connection,
    text: str,
    *,
    limit: int = 5,
    min_score: float = DEFAULT_MIN_SCORE,
    include_hub: bool = True,
) -> dict[str, Any]:
    """"I want to build X" — what do I already have that is close to it?"""
    corpus = Corpus.build(conn, include_hub=include_hub)
    terms, surfaces = _weigh({"name": text, "text": text})
    # The query's own spellings win for display: it is the owner's wording.
    corpus.surfaces.update(surfaces)
    matches = corpus.matches_for(
        terms, limit=limit, min_score=min_score, min_shared=MIN_SHARED_TERMS
    )
    return {
        "query": text,
        "considered": len(corpus.entries),
        "min_score": min_score,
        "matches": [match.to_dict() for match in matches],
    }


def neighbours(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 4,
    min_score: float = DEFAULT_MIN_SCORE,
    include_hub: bool = True,
) -> list[dict[str, Any]]:
    """What else in the portfolio covers the same ground as this project."""
    corpus = Corpus.build(conn, include_hub=include_hub)
    key = f"project:{project_id}"
    entry = next((item for item in corpus.entries if item.key == key), None)
    if entry is None:
        return []
    return [
        match.to_dict()
        for match in corpus.matches_for(
            entry.terms, exclude=key, limit=limit, min_score=min_score,
            min_shared=MIN_SHARED_TERMS,
        )
    ]


def all_neighbours(
    conn: sqlite3.Connection,
    *,
    limit: int = 4,
    min_score: float = DEFAULT_MIN_SCORE,
    include_hub: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Neighbours for every project from one corpus build.

    The dashboard polls the whole portfolio, so building the corpus once per
    project would repeat the same IDF pass for each card.
    """
    corpus = Corpus.build(conn, include_hub=include_hub)
    found: dict[str, list[dict[str, Any]]] = {}
    for entry in corpus.entries:
        if entry.project_id is None:
            continue
        found[entry.project_id] = [
            match.to_dict()
            for match in corpus.matches_for(
                entry.terms, exclude=entry.key, limit=limit, min_score=min_score,
                min_shared=MIN_SHARED_TERMS,
            )
        ]
    return found


def pairs(
    conn: sqlite3.Connection,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    include_hub: bool = False,
) -> list[dict[str, Any]]:
    """Every overlapping pair in the portfolio, strongest first, each once."""
    corpus = Corpus.build(conn, include_hub=include_hub)
    seen: set[frozenset[str]] = set()
    found: list[dict[str, Any]] = []
    for entry in corpus.entries:
        for match in corpus.matches_for(
            entry.terms, exclude=entry.key, limit=len(corpus.entries),
            min_score=min_score, min_shared=MIN_SHARED_TERMS,
        ):
            # Two Hub capabilities overlapping each other is the Hub's business,
            # not the owner's: at least one side must be a Cortex project.
            if entry.source != "project" and match.source != "project":
                continue
            pair = frozenset({entry.key, match.key})
            if pair in seen:
                continue
            seen.add(pair)
            found.append({
                "left": {"key": entry.key, "label": entry.label,
                         "project_id": entry.project_id},
                "right": match.to_dict(),
                "score": match.score,
                "shared": list(match.shared),
            })
    found.sort(key=lambda item: item["score"], reverse=True)
    return found
