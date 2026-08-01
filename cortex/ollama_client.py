"""Thin Ollama client for local, narrow, privacy-sensitive transforms.

Per spec section 10, Ollama handles state regeneration, the secret/privacy
classifier, task-type classification, and brief tailoring. Every call degrades
gracefully: if Ollama is unreachable, callers fall back to deterministic logic.
"""

from __future__ import annotations

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
) -> tuple[str, dict[str, int]]:
    """Run a completion and return normalized token/duration telemetry."""
    host = host or config.ollama_host()
    model = model or config.ollama_model()
    payload = {"model": model, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    try:
        r = requests.post(f"{host}/api/generate", json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        usage = {
            "input_tokens": int(data.get("prompt_eval_count") or 0),
            "output_tokens": int(data.get("eval_count") or 0),
            "total_duration_ns": int(data.get("total_duration") or 0),
        }
        return data.get("response", ""), usage
    except requests.RequestException as exc:
        raise OllamaUnavailable(str(exc)) from exc
