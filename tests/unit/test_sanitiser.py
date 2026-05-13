"""Unit tests for DataSanitiser."""

import pytest

from mithra.storage.sanitiser import DataSanitiser


@pytest.fixture
def sanitiser():
    return DataSanitiser()


class TestDataSanitiser:
    def test_known_pii_replaced_pass1(self, sanitiser):
        text = "Call John Smith on 07700900000"
        result, report = sanitiser.sanitise(text, known_pii={"John Smith": "[NAME]"})
        assert "[NAME]" in result
        assert "John Smith" not in result
        assert report.pass1_hits == 1

    def test_email_stripped_pass2(self, sanitiser):
        text = "Send to alice@example.com for the invoice"
        result, report = sanitiser.sanitise(text)
        assert "alice@example.com" not in result
        assert "[EMAIL]" in result
        assert report.pass2_hits >= 1

    def test_phone_stripped_pass2(self, sanitiser):
        result, report = sanitiser.sanitise("Call +1 555-123-4567 now")
        assert "+1 555-123-4567" not in result

    def test_ssn_stripped(self, sanitiser):
        result, _ = sanitiser.sanitise("SSN: 123-45-6789")
        assert "123-45-6789" not in result
        assert "[SSN]" in result

    def test_clean_text_unchanged(self, sanitiser):
        text = "Buy groceries and call the plumber tomorrow."
        result, report = sanitiser.sanitise(text)
        assert result == text
        assert report.pass1_hits == 0
        assert report.pass2_hits == 0

    def test_both_passes(self, sanitiser):
        text = "John's email is john@corp.com"
        result, report = sanitiser.sanitise(text, known_pii={"John": "[PERSON]"})
        assert "[PERSON]" in result
        assert "john@corp.com" not in result
        assert report.pass1_hits >= 1
        assert report.pass2_hits >= 1
