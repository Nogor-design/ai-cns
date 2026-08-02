"""Thin Ollama client for local, narrow, privacy-sensitive transforms.

Per spec section 10, Ollama handles state regeneration, the secret/privacy
classifier, task-type classification, and brief tailoring. Every call degrades
gracefully: if Ollama is unreachable, callers fall back to deterministic logic.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import requests

from . import config


class OllamaUnavailable(RuntimeError):
    """Raised when the local Ollama server cannot be reached."""


def available(host: str | None = None, timeout: float = 1.5) -> bool:
    host = host or config.ollama_host()
    try:
        r = requests.get(f"{host}/api/tags", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def generate(
    prompt: str,
    *,
    model: str | None = None,
    host: str | None = None,
    system: str | None = None,
    timeout: float = 120.0,
) -> str:
    """Run a single non-streaming completion. Raises OllamaUnavailable on failure."""
    response, _usage = generate_with_usage(
        prompt, model=model, host=host, system=system, timeout=timeout
    )
    return response


def generate_with_usage(
    prompt: str,
    *,
    model: str | None = None,
    host: str | None = None,
    system: str | None = None,
    timeout: float = 120.0,
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, int]]:
    """Run a completion and return normalized token/duration telemetry.

    Passing ``on_chunk`` switches to Ollama's streaming mode so a long local
    generation is visible while it happens rather than only at the end.
    """
    host = host or config.ollama_host()
    model = model or config.ollama_model()
    payload = {"model": model, "prompt": prompt, "stream": bool(on_chunk)}
    if system:
        payload["system"] = system
    try:
        if not on_chunk:
            r = requests.post(f"{host}/api/generate", json=payload, timeout=timeout)
            r.raise_for_status()
            return _unpack(r.json())
        parts: list[str] = []
        final: dict[str, object] = {}
        with requests.post(
            f"{host}/api/generate", json=payload, timeout=timeout, stream=True
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                piece = event.get("response") or ""
                if piece:
                    parts.append(piece)
                    on_chunk(piece)
                if event.get("done"):
                    final = event
        response, usage = _unpack(final)
        return "".join(parts) or response, usage
    except requests.RequestException as exc:
        raise OllamaUnavailable(str(exc)) from exc


def _unpack(data: dict[str, object]) -> tuple[str, dict[str, int]]:
    usage = {
        "input_tokens": int(data.get("prompt_eval_count") or 0),
        "output_tokens": int(data.get("eval_count") or 0),
        "total_duration_ns": int(data.get("total_duration") or 0),
    }
    return str(data.get("response", "")), usage
