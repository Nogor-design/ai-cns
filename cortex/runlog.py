"""Append-only live output for a run.

A worker can take many minutes, and previously nothing was visible until the
process exited. Each run now streams into its own file as it produces output,
so the dashboard can tail it, and so the transcript survives a dashboard
restart or a crashed worker.

Files are plain UTF-8 text addressed by byte offset. That is deliberately
boring: tailing is a seek and a read, with no shared in-memory buffer to
synchronise between the worker thread and the HTTP threads serving it.
"""

from __future__ import annotations

import threading
from pathlib import Path

from . import config

# Guards creation and appends. Writes are small and infrequent relative to a
# model's thinking time, so one lock across runs is not a bottleneck.
_LOCK = threading.Lock()

# Hard ceiling per run so a runaway worker cannot fill the disk.
MAX_BYTES = 8 * 1024 * 1024


def log_dir() -> Path:
    return config.run_root() / "logs"


def path(run_id: str) -> Path:
    return log_dir() / f"{run_id}.log"


def start(run_id: str, header: str = "") -> Path:
    """Create (or truncate) the log for a run and return its path."""
    target = path(run_id)
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _open(target, "w") as handle:
            handle.write(header)
    return target


def _open(target: Path, mode: str):
    """Open a log without newline translation.

    Reads are by byte offset, so letting Windows expand "\\n" to "\\r\\n" on
    write would make every offset drift against what was actually written.
    """
    return target.open(mode, encoding="utf-8", errors="replace", newline="")


def append(run_id: str, text: str) -> None:
    if not text:
        return
    target = path(run_id)
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if target.exists() and target.stat().st_size >= MAX_BYTES:
                return
        except OSError:
            pass
        with _open(target, "a") as handle:
            handle.write(text)


def read_from(run_id: str, offset: int = 0) -> tuple[str, int]:
    """Return text written after `offset`, plus the new offset.

    Reading by byte offset can split a multi-byte character, so decoding
    replaces malformed tails rather than raising; the next read picks up the
    remaining bytes.
    """
    target = path(run_id)
    if not target.is_file():
        return "", offset
    try:
        with target.open("rb") as handle:
            handle.seek(max(0, offset))
            chunk = handle.read()
            return chunk.decode("utf-8", errors="replace"), handle.tell()
    except OSError:
        return "", offset


def size(run_id: str) -> int:
    try:
        return path(run_id).stat().st_size
    except OSError:
        return 0
