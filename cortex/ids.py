"""ID and timestamp helpers."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone


def now() -> str:
    """UTC timestamp in ISO-8601 seconds precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def short_id() -> str:
    """Short random id for tasks/runs/decisions."""
    return uuid.uuid4().hex[:12]


def slugify(name: str) -> str:
    """Stable, human-readable project id from a name."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "project"
