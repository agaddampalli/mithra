"""
DataSanitiser — strips PII from data before it is sent to cloud LLM calls.

Applied before EVERY cloud LLM call via LLMRouter. Not optional.

Two-pass approach:
  Pass 1: known PII from ContextSnapshot (high confidence, exact match).
          known_pii entries are sorted by length descending so longer values
          (e.g. "John Smith") are replaced before shorter substrings ("John").
  Pass 2: regex pattern matching for unknown PII — phones, emails, card numbers,
          SSNs, dollar amounts.

A SanitisationReport is returned alongside the sanitised text so the
caller can log what was stripped and verify the process ran.

DataSanitiser is used exclusively by LLMRouter. Domain code never calls it
directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class SanitisationReport:
    """Record of what was stripped in a sanitisation pass."""

    replacements: list[tuple[str, str]] = field(default_factory=list)
    pass1_hits: int = 0
    pass2_hits: int = 0


# Patterns applied in order during Pass 2.
# Each entry: (compiled pattern, replacement token).
# Ordered so more specific patterns run before broader ones.
_PII_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r'\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'),
        '[PHONE]',
    ),
    (
        re.compile(r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b'),
        '[EMAIL]',
    ),
    (
        re.compile(r'\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b'),
        '[CARD]',
    ),
    (
        re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
        '[SSN]',
    ),
    (
        re.compile(r'\$\s?\d{1,3}(,\d{3})*(\.\d{2})?\b'),
        '[AMOUNT]',
    ),
]


class DataSanitiser:
    """Strips PII from text before cloud LLM submission."""

    def sanitise(
        self, text: str, known_pii: dict[str, str] | None = None
    ) -> tuple[str, SanitisationReport]:
        """Return sanitised text and a report of what was replaced.

        Pass 1 uses known_pii (exact string replacement, longest match first).
        Pass 2 applies regex patterns for residual unknown PII.
        Never raises — on any error returns the original text with an empty report.

        Args:
            text: Raw text to sanitise.
            known_pii: Map of actual value → replacement token from ContextSnapshot.
        """
        report = SanitisationReport()
        try:
            result = text

            # Pass 1 — known PII, sorted longest-first to prevent partial matches
            if known_pii:
                for actual, token in sorted(known_pii.items(), key=lambda x: len(x[0]), reverse=True):
                    if actual and actual in result:
                        result = result.replace(actual, token)
                        report.replacements.append((actual, token))
                        report.pass1_hits += 1

            # Pass 2 — regex patterns for unknown PII
            for pattern, token in _PII_PATTERNS:
                matches = re.findall(pattern, result)
                if matches:
                    result = re.sub(pattern, token, result)
                    report.pass2_hits += len(matches)
                    report.replacements += [(m, token) for m in matches]

            return result, report
        except Exception:
            return text, SanitisationReport()
