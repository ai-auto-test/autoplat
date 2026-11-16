"""Secrets and evidence sanitization (Chapter 15)."""
from __future__ import annotations

import os
import re
from typing import Protocol

RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)\s*[=:]\s*\S+"),
     r"\1=<redacted>"),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1<redacted>@"),
    (re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b"), "<vin>"),
    (re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b"), "<ip>"),
]

# Destination profiles: which rules apply where. Index into RULES.
PROFILES: dict[str, list[int]] = {
    "lab": [0, 1],             # secrets only; internal addresses stay useful in the lab
    "external": [0, 1, 2, 3],  # suppliers, AI services, anything outside the lab
}


def sanitize(text: str, extra_secrets: list[str] | None = None, profile: str = "external") -> str:
    for secret in sorted(extra_secrets or [], key=len, reverse=True):
        if secret:
            text = text.replace(secret, "<redacted>")
    for i in PROFILES[profile]:
        pattern, replacement = RULES[i]
        text = pattern.sub(replacement, text)
    return text


class Redactor:
    """Masks every registered secret wherever it appears in evidence."""

    def __init__(self, profile: str = "lab"):
        self.profile = profile
        self._secrets: set[str] = set()

    def register(self, value: str) -> None:
        if value and len(value) >= 4:
            self._secrets.add(value)

    def redact(self, text: str, profile: str | None = None) -> str:
        return sanitize(text, list(self._secrets), profile or self.profile)


redactor = Redactor()


class SecretClient(Protocol):
    def read(self, path: str) -> str: ...


class EnvSecretClient:
    """Reads `scope/name` from environment variable AUTOPLAT_SECRET_<SCOPE>_<NAME>."""

    def read(self, path: str) -> str:
        key = "AUTOPLAT_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", path).upper()
        try:
            return os.environ[key]
        except KeyError:
            raise KeyError(f"secret {path!r} not available (set {key})") from None


class SecretProvider:
    def __init__(self, client: SecretClient, scope: str, redact: Redactor = redactor):
        self._client, self._scope, self._redactor = client, scope, redact

    def get(self, name: str) -> str:
        value = self._client.read(f"{self._scope}/{name}")
        self._redactor.register(value)  # masked wherever it appears in evidence
        return value
