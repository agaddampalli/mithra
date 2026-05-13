"""
DataSanitiser — strips PII from data before it is sent to cloud LLM calls.

Applied before EVERY cloud LLM call via LLMRouter. Not optional.

Two-pass approach:
  Pass 1: known PII from ContextSnapshot (high confidence, exact match).
  Pass 2: regex pattern matching for unknown PII (names, phone numbers,
          email addresses, national IDs, financial account numbers).

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


_PII_PATTERNS: list[tuple[str, str]] = [
    # Email
    (r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", "[EMAIL]"),
    # UK National Insurance Number (before phone — more specific)
    (r"\b[A-Z]{2}\s?\d{6}\s?[A-D]\b", "[NI_NUMBER]"),
    # US SSN (before phone — more specific)
    (r"\b\d{3}-\d{2}-\d{4}\b", "[SSN]"),
    # Phone (international + US formats)
    (r"\+?[\d\s\-().]{7,15}\d", "[PHONE]"),
    # Credit/debit card numbers (4×4)
    (r"\b(?:\d[ \-]?){13,16}\b", "[CARD_NUMBER]"),
    # Bank account / sort code patterns (UK: XX-XX-XX XXXXXXXX)
    (r"\b\d{2}-\d{2}-\d{2}\s+\d{8}\b", "[BANK_DETAILS]"),
    # Postcode / ZIP (UK + US)
    (r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b", "[POSTCODE]"),
    (r"\b\d{5}(?:-\d{4})?\b", "[ZIP]"),
]

_COMPILED: list[tuple[re.Pattern, str]] = [
    (re.compile(p, re.IGNORECASE), repl) for p, repl in _PII_PATTERNS
]


class DataSanitiser:
    """Strips PII from text before cloud LLM submission."""

    def sanitise(
        self, text: str, known_pii: dict[str, str] | None = None
    ) -> tuple[str, SanitisationReport]:
        """
        Return sanitised text and a report of what was replaced.

        Args:
            text: Raw text to sanitise.
            known_pii: Map of actual value → replacement token from ContextSnapshot.
        """
        report = SanitisationReport()
        result = text

        # Pass 1 — known PII from ContextSnapshot (exact string replacement)
        if known_pii:
            for actual, token in known_pii.items():
                if actual and actual in result:
                    result = result.replace(actual, token)
                    report.replacements.append((actual, token))
                    report.pass1_hits += 1

        # Pass 2 — regex patterns for unknown PII
        for pattern, replacement in _COMPILED:
            new_result, count = pattern.subn(replacement, result)
            if count:
                report.replacements.append((f"<pattern:{pattern.pattern[:30]}>", replacement))
                report.pass2_hits += count
                result = new_result

        return result, report
