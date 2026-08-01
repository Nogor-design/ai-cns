"""The local secret/privacy scan."""

from __future__ import annotations

import pytest

from cortex import secrets_scan


@pytest.mark.parametrize(
    "text,kind",
    [
        ("AKIAIOSFODNN7EXAMPLE", "aws_access_key_id"),
        ("token = sk-abcdefghijklmnopqrstuvwxyz0123", "openai_api_key"),
        ("key: AIza" + "B" * 35, "google_api_key"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private_key_block"),
        ("password = 'hunter2hunter2hunter2'", "generic_secret_assignment"),
        ("ssn 123-45-6789 on file", "us_ssn"),
    ],
)
def test_regex_detects(text, kind):
    findings = secrets_scan.regex_scan(text)
    assert any(f.kind == kind for f in findings), (text, findings)


def test_clean_text_passes():
    text = "# Project State\n\nThis is a normal brief about building a CLI tool.\n"
    assert secrets_scan.regex_scan(text) == []
    assert secrets_scan.is_clean(text, use_ollama=False)


def test_findings_are_redacted():
    findings = secrets_scan.regex_scan("AKIAIOSFODNN7EXAMPLE")
    assert findings
    # Never echo the whole secret back.
    assert "AKIAIOSFODNN7EXAMPLE" not in findings[0].snippet
    assert "***" in findings[0].snippet


def test_dedup_same_snippet():
    text = "AKIAIOSFODNN7EXAMPLE\nAKIAIOSFODNN7EXAMPLE\n"
    findings = secrets_scan.regex_scan(text)
    assert len(findings) == 1
