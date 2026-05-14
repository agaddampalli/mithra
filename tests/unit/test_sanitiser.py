"""Unit tests for DataSanitiser."""

import pytest

from mithra.storage.sanitiser import DataSanitiser, SanitisationReport


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


# ---------------------------------------------------------------------------
# Pass 1 — known PII
# ---------------------------------------------------------------------------


class TestPass1KnownPii:
    def test_known_name_replaced_by_token(self, sanitiser):
        result, report = sanitiser.sanitise(
            "Contact Alice today", known_pii={"Alice": "[PERSON_ALICE]"}
        )
        assert "Alice" not in result
        assert "[PERSON_ALICE]" in result
        assert report.pass1_hits == 1

    def test_longer_match_takes_precedence_over_shorter(self, sanitiser):
        # "John Smith" must be replaced before "John" to avoid leaving "Smith"
        result, report = sanitiser.sanitise(
            "Message from John Smith please",
            known_pii={"John": "[FIRST]", "John Smith": "[FULL_NAME]"},
        )
        assert "John Smith" not in result
        assert "[FULL_NAME]" in result
        # "[FIRST]" should NOT appear because "John Smith" was consumed first
        assert "[FIRST]" not in result

    def test_known_pii_none_skips_pass1(self, sanitiser):
        _, report = sanitiser.sanitise("Hello there", known_pii=None)
        assert report.pass1_hits == 0

    def test_known_pii_empty_dict_skips_pass1(self, sanitiser):
        _, report = sanitiser.sanitise("Hello there", known_pii={})
        assert report.pass1_hits == 0

    def test_multiple_known_pii_entries_all_replaced(self, sanitiser):
        text = "Alice called Bob about the meeting"
        result, report = sanitiser.sanitise(
            text, known_pii={"Alice": "[P1]", "Bob": "[P2]"}
        )
        assert "Alice" not in result
        assert "Bob" not in result
        assert "[P1]" in result
        assert "[P2]" in result
        assert report.pass1_hits == 2

    def test_pass1_value_not_in_text_not_counted(self, sanitiser):
        _, report = sanitiser.sanitise(
            "Nothing here", known_pii={"Alice": "[P1]"}
        )
        assert report.pass1_hits == 0

    def test_pass1_replacement_recorded_in_replacements(self, sanitiser):
        _, report = sanitiser.sanitise(
            "Call Jane", known_pii={"Jane": "[JANE]"}
        )
        assert ("Jane", "[JANE]") in report.replacements


# ---------------------------------------------------------------------------
# Pass 2 — regex patterns
# ---------------------------------------------------------------------------


class TestPass2Phone:
    def test_us_phone_with_country_code(self, sanitiser):
        result, report = sanitiser.sanitise("Call +1 555-123-4567")
        assert "[PHONE]" in result
        assert report.pass2_hits >= 1

    def test_us_phone_without_country_code(self, sanitiser):
        result, _ = sanitiser.sanitise("ring 555.123.4567 now")
        assert "[PHONE]" in result

    def test_us_phone_parentheses_format(self, sanitiser):
        result, _ = sanitiser.sanitise("number: (555) 123-4567")
        assert "[PHONE]" in result

    def test_phone_not_triggered_on_short_digits(self, sanitiser):
        result, report = sanitiser.sanitise("order number 12345")
        assert report.pass2_hits == 0


class TestPass2Email:
    def test_simple_email_replaced(self, sanitiser):
        result, report = sanitiser.sanitise("email me at user@domain.com")
        assert "user@domain.com" not in result
        assert "[EMAIL]" in result
        assert report.pass2_hits >= 1

    def test_email_with_plus_tag(self, sanitiser):
        result, _ = sanitiser.sanitise("send to user+tag@example.org")
        assert "[EMAIL]" in result

    def test_email_with_subdomain(self, sanitiser):
        result, _ = sanitiser.sanitise("contact first.last@mail.company.co.uk")
        assert "[EMAIL]" in result


class TestPass2Card:
    def test_card_number_plain(self, sanitiser):
        result, report = sanitiser.sanitise("card 4111111111111111 on file")
        assert "4111111111111111" not in result
        assert "[CARD]" in result
        assert report.pass2_hits >= 1

    def test_card_number_with_spaces(self, sanitiser):
        result, _ = sanitiser.sanitise("card: 4111 1111 1111 1111")
        assert "[CARD]" in result

    def test_card_number_with_dashes(self, sanitiser):
        result, _ = sanitiser.sanitise("charge 4111-1111-1111-1111")
        assert "[CARD]" in result


class TestPass2SSN:
    def test_ssn_replaced(self, sanitiser):
        result, report = sanitiser.sanitise("SSN is 123-45-6789")
        assert "123-45-6789" not in result
        assert "[SSN]" in result
        assert report.pass2_hits >= 1

    def test_ssn_distinct_from_phone_pattern(self, sanitiser):
        # SSN is 9 digits (XXX-XX-XXXX); should be caught by SSN pattern
        result, _ = sanitiser.sanitise("ref 987-65-4321")
        assert "[SSN]" in result


class TestPass2Amount:
    def test_dollar_amount_integer(self, sanitiser):
        result, report = sanitiser.sanitise("paid $500 today")
        assert "$500" not in result
        assert "[AMOUNT]" in result
        assert report.pass2_hits >= 1

    def test_dollar_amount_with_cents(self, sanitiser):
        result, _ = sanitiser.sanitise("total is $1,234.56")
        assert "[AMOUNT]" in result

    def test_dollar_amount_with_space_after_sign(self, sanitiser):
        result, _ = sanitiser.sanitise("cost $ 99.99")
        assert "[AMOUNT]" in result


# ---------------------------------------------------------------------------
# Combined passes
# ---------------------------------------------------------------------------


class TestCombinedPasses:
    def test_pass1_then_pass2_both_fire(self, sanitiser):
        text = "Alice's card is 4111 1111 1111 1111"
        result, report = sanitiser.sanitise(
            text, known_pii={"Alice": "[ALICE]"}
        )
        assert "Alice" not in result
        assert "[ALICE]" in result
        assert "[CARD]" in result
        assert report.pass1_hits == 1
        assert report.pass2_hits >= 1

    def test_pass1_token_not_double_matched_by_pass2(self, sanitiser):
        # known_pii replaces "john@corp.com" before pass2 sees it
        text = "email john@corp.com"
        result, report = sanitiser.sanitise(
            text, known_pii={"john@corp.com": "[EMAIL_JOHN]"}
        )
        assert result == "email [EMAIL_JOHN]"
        assert report.pass1_hits == 1
        assert report.pass2_hits == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_text_returns_empty_and_empty_report(self, sanitiser):
        result, report = sanitiser.sanitise("")
        assert result == ""
        assert report.pass1_hits == 0
        assert report.pass2_hits == 0
        assert report.replacements == []

    def test_text_with_no_pii_unchanged(self, sanitiser):
        text = "The weather today is sunny and warm."
        result, report = sanitiser.sanitise(text)
        assert result == text
        assert report.pass1_hits == 0
        assert report.pass2_hits == 0

    def test_never_raises_on_unexpected_input(self, sanitiser):
        result, report = sanitiser.sanitise(None)  # type: ignore[arg-type]
        assert result is None
        assert report.pass1_hits == 0

    def test_pass1_hits_counts_once_per_key_not_per_occurrence(self, sanitiser):
        # replace() replaces all occurrences; hit is counted once per key
        _, report = sanitiser.sanitise(
            "Alice and Bob and Alice again",
            known_pii={"Alice": "[P1]", "Bob": "[P2]"},
        )
        assert report.pass1_hits == 2

    def test_pass2_hits_counts_multiple_matches(self, sanitiser):
        text = "user1@a.com and user2@b.com"
        _, report = sanitiser.sanitise(text)
        assert report.pass2_hits == 2

    def test_sanitisation_report_dataclass_defaults(self):
        report = SanitisationReport()
        assert report.replacements == []
        assert report.pass1_hits == 0
        assert report.pass2_hits == 0
