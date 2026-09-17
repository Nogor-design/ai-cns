"""Local compute lanes: what the local models can actually run, and when.

This machine has one GPU, so every local model shares one slot: two local runs
at once would push each other off the GPU and both crawl. Models are classified
from their real size and architecture rather than their name:

* ``gpu``          -- fits in GPU memory with room for context; fast.
* ``hybrid_moe``   -- mixture-of-experts larger than the GPU; only a few experts
                      run per token, so CPU offload stays usable.
* ``hybrid_dense`` -- dense and somewhat larger than the GPU; slow but workable.
* ``cpu_batch``    -- very large mixture-of-experts; overnight/batch only.
* ``avoid``        -- too large to be useful in an agent loop here.
* ``embedding``    -- embedding models, not for agent work.

NinjaTrader uses the CPU performance cores during backtests and optimizations.
While it is running, unattended local work is restricted to ``gpu`` models so
research runs are not slowed down by an agent.
"""

from __future__ import annotations

import ctypes
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import requests

from . import config, ids

GPU_FIT_FRACTION = 0.8
DENSE_HYBRID_FACTOR = 1.35
CPU_BATCH_FACTOR = 4.0
NINJATRADER_IMAGE = "NinjaTrader.exe"
BENCH_PROMPT = (
    "You are reviewing a small Python function for correctness.\n\n"
    "def moving_average(values, window):\n"
    "    if window <= 0:\n"
    "        raise ValueError('window must be positive')\n"
    "    out = []\n"
    "    for i in range(len(values) - window):\n"
    "        out.append(sum(values[i:i + window]) / window)\n"
    "    return out\n\n"
    "List every bug you find, with a one-line fix for each. Be concise."
)

_SHOW_CACHE: dict[str, dict[str, Any]] = {}
_SIGNAL_CACHE: dict[str, tuple[float, Any]] = {}


@dataclass(frozen=True)
class Hardware:
    gpu_name: str | None
    vram_mb: int | None
    vram_used_mb: int | None
    gpu_util_pct: int | None
    ram_mb: int | None


def _cached(key: str, max_age: float, compute):  # type: ignore[no-untyped-def]
    hit = _SIGNAL_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    value = compute()
    _SIGNAL_CACHE[key] = (time.monotonic(), value)
    return value


def hardware(max_age: float = 15) -> Hardware:
    return _cached("hardware", max_age, _read_hardware)


def _read_hardware() -> Hardware:
    name = total = used = util = None
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parts = [part.strip() for part in proc.stdout.splitlines()[0].split(",")]
            name, total, used, util = parts[0], int(parts[1]), int(parts[2]), int(parts[3])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass
    return Hardware(name, total, used, util, _ram_mb())


def _ram_mb() -> int | None:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return int(status.ullTotalPhys // (1024 * 1024))
        return None
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        return None


def ninjatrader_running(max_age: float = 15) -> bool:
    return _cached("ninjatrader", max_age, _read_ninjatrader)


def _read_ninjatrader() -> bool:
    if os.name != "nt":
        return False
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {NINJATRADER_IMAGE}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return NINJATRADER_IMAGE.lower() in proc.stdout.lower()


# -------------------------------------------------------------- models ---
def classify(
    *, size_bytes: int, family: str | None, is_moe: bool,
    vram_mb: int | None, ram_mb: int | None,
) -> str:
    fam = (family or "").lower()
    if "bert" in fam or "embed" in fam:
        return "embedding"
    if not vram_mb:
        return "cpu_batch" if is_moe else "avoid"
    size_mb = size_bytes / (1024 * 1024)
    ram_budget = (ram_mb or 0) * 0.5
    if size_mb <= vram_mb * GPU_FIT_FRACTION:
        return "gpu"
    if is_moe:
        if size_mb > vram_mb + ram_budget:
            return "avoid"
        return "cpu_batch" if size_mb > vram_mb * CPU_BATCH_FACTOR else "hybrid_moe"
    if size_mb <= vram_mb * DENSE_HYBRID_FACTOR:
        return "hybrid_dense"
    return "avoid"


def _show(model: str, digest: str) -> dict[str, Any]:
    key = f"{model}@{digest}"
    if key not in _SHOW_CACHE:
        try:
            response = requests.post(
                f"{config.ollama_host()}/api/show", json={"model": model}, timeout=5
            )
            _SHOW_CACHE[key] = response.json() if response.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            _SHOW_CACHE[key] = {}
    return _SHOW_CACHE[key]


def models(max_age: float = 60) -> list[dict[str, Any]]:
    return _cached("models", max_age, _read_models)


def _read_models() -> list[dict[str, Any]]:
    try:
        response = requests.get(f"{config.ollama_host()}/api/tags", timeout=3)
        tags = response.json().get("models", []) if response.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return []
    hw = hardware()
    rows = []
    for tag in tags:
        name = str(tag.get("name") or tag.get("model") or "")
        details = tag.get("details") or {}
        info = _show(name, str(tag.get("digest") or "")).get("model_info") or {}
        is_moe = any(key.endswith(".expert_count") and value for key, value in info.items())
        lane = classify(
            size_bytes=int(tag.get("size") or 0),
            family=details.get("family"),
            is_moe=is_moe,
            vram_mb=hw.vram_mb,
            ram_mb=hw.ram_mb,
        )
        rows.append({
            "name": name,
            "size_gb": round(int(tag.get("size") or 0) / 1024**3, 1),
            "family": details.get("family"),
            "parameters": details.get("parameter_size"),
            "quantization": details.get("quantization_level"),
            "moe": is_moe,
            "lane": lane,
        })
    rows.sort(key=lambda row: (row["name"].count("/"), row["size_gb"]))
    return rows


def loaded_models() -> list[dict[str, Any]]:
    try:
        response = requests.get(f"{config.ollama_host()}/api/ps", timeout=3)
        data = response.json().get("models", []) if response.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return []
    rows = []
    for item in data:
        size = int(item.get("size") or 0)
        vram = int(item.get("size_vram") or 0)
        rows.append({
            "name": item.get("name"),
            "size_gb": round(size / 1024**3, 1),
            "gpu_percent": round(vram / size * 100) if size else None,
            "context": item.get("context_length"),
            "expires_at": item.get("expires_at"),
        })
    return rows


def lane_for(model: str) -> str | None:
    bare = model.split("/", 1)[1] if model.startswith("ollama/") else model
    for row in models():
        if row["name"] == bare or row["name"] == f"{bare}:latest":
            return row["lane"]
    return None


# ----------------------------------------------------------- admission ---
# Runs never marked finished (a crashed dashboard) must not hold the slot
# forever; dispatch timeouts are far shorter than this. Phase 2 adds leases.
IN_FLIGHT_HORIZON_HOURS = 3


def local_runs_in_flight(conn: sqlite3.Connection) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM runs WHERE ended_at IS NULL
           AND (model LIKE 'ollama:%' OR model LIKE 'opencode-local:%')
           AND started_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)""",
        (f"-{IN_FLIGHT_HORIZON_HOURS} hours",),
    ).fetchone()[0]


def admit(conn: sqlite3.Connection, model: str) -> tuple[bool, str]:
    """May an unattended local run of ``model`` start now?"""
    if local_runs_in_flight(conn):
        return False, "The local model slot is busy; one local run at a time."
    lane = lane_for(model)
    if lane is None:
        return False, f"{model} is not installed in Ollama."
    if lane in {"avoid", "embedding"}:
        return False, f"{model} is classified '{lane}' on this machine."
    if lane == "cpu_batch":
        return False, f"{model} is a batch-only model; not used for unattended agent work."
    if lane != "gpu" and ninjatrader_running():
        return False, f"NinjaTrader is running; {model} ({lane}) would compete for CPU cores."
    return True, f"{model} runs in the {lane} lane."


# ----------------------------------------------------------- benchmark ---
def benchmark(conn: sqlite3.Connection, model: str, *, timeout: float = 900) -> dict[str, Any]:
    """Measure one short generation and record it."""
    started = time.monotonic()
    row: dict[str, Any] = {
        "model": model, "lane": lane_for(model),
        "ninjatrader_running": int(ninjatrader_running(max_age=0)),
    }
    try:
        response = requests.post(
            f"{config.ollama_host()}/api/generate",
            json={"model": model, "prompt": BENCH_PROMPT, "stream": False,
                  "options": {"num_predict": 160, "temperature": 0}},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        row.update({
            "prompt_tokens": data.get("prompt_eval_count"),
            "output_tokens": data.get("eval_count"),
            "prompt_tps": _rate(data.get("prompt_eval_count"), data.get("prompt_eval_duration")),
            "output_tps": _rate(data.get("eval_count"), data.get("eval_duration")),
            "load_seconds": round((data.get("load_duration") or 0) / 1e9, 2),
            "total_seconds": round((data.get("total_duration") or 0) / 1e9, 2),
        })
        for loaded in loaded_models():
            if loaded["name"] in {model, f"{model}:latest"}:
                row["gpu_percent"] = loaded["gpu_percent"]
    except (requests.RequestException, ValueError) as exc:
        row["error"] = str(exc)[:500]
        row["total_seconds"] = round(time.monotonic() - started, 2)
    row["measured_at"] = ids.now()
    columns = [
        "model", "lane", "prompt_tokens", "output_tokens", "prompt_tps", "output_tps",
        "load_seconds", "total_seconds", "gpu_percent", "ninjatrader_running",
        "error", "measured_at",
    ]
    conn.execute(
        f"INSERT INTO local_benchmarks ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        tuple(row.get(column) for column in columns),
    )
    conn.commit()
    return row


def _rate(count: Any, duration_ns: Any) -> float | None:
    try:
        return round(float(count) / (float(duration_ns) / 1e9), 1) if duration_ns else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def latest_benchmarks(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT b.* FROM local_benchmarks b
           JOIN (SELECT model, MAX(id) AS id FROM local_benchmarks GROUP BY model) latest
             ON latest.id = b.id"""
    ).fetchall()
    return {row["model"]: dict(row) for row in rows}


#: Lanes that can never run unattended agent work here, so listing them only
#: makes the local inventory harder to read. ``admit`` refuses them as well.
UNUSABLE_LANES = {"avoid", "embedding"}


def payload(conn: sqlite3.Connection, *, include_unusable: bool = False) -> dict[str, Any]:
    hw = hardware()
    benches = latest_benchmarks(conn)
    full = models()
    catalog = full if include_unusable else [
        row for row in full if row.get("lane") not in UNUSABLE_LANES
    ]
    for row in catalog:
        bench = benches.get(row["name"])
        row["output_tps"] = bench.get("output_tps") if bench else None
        row["benchmarked_at"] = bench.get("measured_at") if bench else None
    return {
        "hardware": hw.__dict__,
        "ninjatrader_running": ninjatrader_running(),
        "slot_busy": bool(local_runs_in_flight(conn)),
        "loaded": loaded_models(),
        "models": catalog,
        "hidden_models": [
            {"name": row["name"], "lane": row["lane"], "size_gb": row.get("size_gb")}
            for row in full if row.get("lane") in UNUSABLE_LANES
        ],
    }
