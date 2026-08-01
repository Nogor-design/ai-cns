"""API chat capture (spec section 7, API mode).

Calls a model API directly with the compiled brief and returns the response
text. Providers are selected by model name and configured via environment
variables. Designed to be injectable: `runs.start_run` accepts an `api_caller`
so tests never touch the network.
"""

from __future__ import annotations

import os

import requests


class APIUnavailable(RuntimeError):
    """Raised when no provider is configured or the call fails."""


def call(model: str, brief: str, *, timeout: float = 120.0) -> str:
    """Dispatch to a provider based on the model name."""
    name = (model or "").lower()
    if name.startswith("gemini"):
        return _gemini(model, brief, timeout)
    if name.startswith(("claude", "anthropic")):
        return _anthropic(model, brief, timeout)
    if name.startswith(("gpt", "o1", "o3", "openai")):
        return _openai(model, brief, timeout)
    raise APIUnavailable(
        f"no API provider wired for model '{model}'. "
        "Use --mode manual, or configure a supported provider."
    )


def _require(env: str) -> str:
    val = os.environ.get(env)
    if not val:
        raise APIUnavailable(f"missing environment variable: {env}")
    return val


def _gemini(model: str, brief: str, timeout: float) -> str:
    key = _require("GEMINI_API_KEY")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={key}"
    )
    payload = {"contents": [{"parts": [{"text": brief}]}]}
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (requests.RequestException, KeyError, IndexError) as exc:
        raise APIUnavailable(f"gemini call failed: {exc}") from exc


def _anthropic(model: str, brief: str, timeout: float) -> str:
    key = _require("ANTHROPIC_API_KEY")
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": brief}],
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        return "".join(
            block.get("text", "") for block in data.get("content", [])
        )
    except (requests.RequestException, KeyError) as exc:
        raise APIUnavailable(f"anthropic call failed: {exc}") from exc


def _openai(model: str, brief: str, timeout: float) -> str:
    key = _require("OPENAI_API_KEY")
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": [{"role": "user", "content": brief}]},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, IndexError) as exc:
        raise APIUnavailable(f"openai call failed: {exc}") from exc
