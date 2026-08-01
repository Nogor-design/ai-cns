"""Local secret / privacy scan (spec section 6, step 4).

Cheap, local, high-value given trading/recruiting data. Regex pass catches the
common, high-confidence secret shapes; an optional Ollama classifier can flag
fuzzier PII when available. The brief compiler blocks on any finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import ollama_client


@dataclass(frozen=True)
class Finding:
    kind: str
    snippet: str
    line: int


# High-confidence patterns. Each is (kind, compiled regex).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=]{20,}\b")),
    ("generic_secret_assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|client[_-]?secret)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+]{12,}['\"]?"
    )),
    ("us_ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
]


def regex_scan(text: str) -> list[Finding]:
    """Deterministic regex pass. Returns all findings, deduplicated by snippet."""
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, pat in _PATTERNS:
            for m in pat.finditer(line):
                snippet = _redact(m.group(0))
                key = (kind, snippet)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(Finding(kind=kind, snippet=snippet, line=lineno))
    return findings


def _redact(s: str) -> str:
    """Show only enough to identify the hit, never the full secret."""
    s = s.strip()
    if len(s) <= 8:
        return s[:2] + "***"
    return s[:4] + "***" + s[-2:]


def scan(text: str, *, use_ollama: bool = True) -> list[Finding]:
    """Full scan: regex always; Ollama PII classifier when reachable.

    The Ollama pass is best-effort and additive — its absence never weakens the
    regex guarantees.
    """
    findings = regex_scan(text)
    if use_ollama and ollama_client.available():
        try:
            extra = _ollama_pii(text)
            findings.extend(extra)
        except ollama_client.OllamaUnavailable:
            pass
    return findings


def _ollama_pii(text: str) -> list[Finding]:
    prompt = (
        "You are a privacy scanner. Examine the TEXT below and answer with a "
        "single word: YES if it contains personal identifiable information such "
        "as client names tied to financial data, home addresses, phone numbers, "
        "or government IDs; otherwise NO.\n\nTEXT:\n" + text[:4000]
    )
    resp = ollama_client.generate(prompt).strip().upper()
    if resp.startswith("YES"):
        return [Finding(kind="ollama_pii", snippet="(model-flagged PII)", line=0)]
    return []


def is_clean(text: str, *, use_ollama: bool = True) -> bool:
    return not scan(text, use_ollama=use_ollama)
