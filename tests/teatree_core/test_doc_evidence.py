"""Field-context evidence probe for generated-doc / export verification (#2296).

The probe in :mod:`teatree.core.doc_evidence` binds a generated-doc/export
evidence assertion to a NAMED structured anchor — a field or a table column —
so an AC-constrained term must be found in the field/row the AC constrains, NOT
anywhere on the page. The canonical false-positive these tests pin: the term
"Leasehold" appears only in the borrower NAME field ("E2E Leasehold"), with no
security-type row carrying it. A page-wide substring scan passes (false
"verified"); the field-context probe correctly rejects.

Pure string logic over a parsed :class:`StructuredDoc` — no ORM, host, or
network — so it unit-tests in isolation.
"""

import pytest

from teatree.core.doc_evidence import (
    ColumnClaim,
    DocEvidenceError,
    FieldClaim,
    StructuredDoc,
    check_doc_evidence,
    field_contains,
    reject_page_wide_substring,
    row_cell_contains,
)

# The recurrence fixture: a loan document whose BORROWER NAME embeds the feature
# keyword ("Leasehold"), but whose Security table carries a DIFFERENT type. A
# page-wide scan for "Leasehold" hits the name; the structured Security row does
# not. This is the exact AC8 shape that produced the false-verified PDF.
_FALSE_POSITIVE_DOC = StructuredDoc(
    fields={"Borrower": "E2E Leasehold", "Amount": "100000"},
    rows=[{"Type": "Mortgage", "Value": "100000"}],
)

# The true-positive: the Security row's Type cell genuinely carries "Leasehold".
_TRUE_POSITIVE_DOC = StructuredDoc(
    fields={"Borrower": "Jane Doe", "Amount": "100000"},
    rows=[{"Type": "Leasehold", "Value": "100000"}],
)

# A flattened page-text rendering of the false-positive doc — what a naive
# `pdftotext`-style extractor yields and a page-wide substring check runs over.
_FALSE_POSITIVE_PAGE_TEXT = "Borrower: E2E Leasehold\nAmount: 100000\nSecurity Type: Mortgage\nValue: 100000"


class TestPageWideSubstringIsTheFalsePositive:
    """The RED baseline: a page-wide substring scan FALSE-verifies the recurrence."""

    def test_page_wide_substring_matches_the_borrower_name(self) -> None:
        # The bug: `term in full_text` passes because the term is in the borrower name.
        assert "Leasehold".casefold() in _FALSE_POSITIVE_PAGE_TEXT.casefold()

    def test_reject_page_wide_substring_refuses_even_when_present(self) -> None:
        with pytest.raises(DocEvidenceError) as exc:
            reject_page_wide_substring(_FALSE_POSITIVE_PAGE_TEXT, "Leasehold")
        assert "page-wide" in str(exc.value).lower()

    def test_reject_page_wide_substring_refuses_when_absent_too(self) -> None:
        with pytest.raises(DocEvidenceError):
            reject_page_wide_substring("Borrower: Jane Doe", "Leasehold")


class TestColumnClaimRejectsTheFalsePositive:
    """The field-context gate rejects the recurrence the page-wide scan accepted."""

    def test_term_only_in_borrower_name_is_not_verified(self) -> None:
        claim = ColumnClaim(term="Leasehold", column_label="Type")
        with pytest.raises(DocEvidenceError) as exc:
            check_doc_evidence(_FALSE_POSITIVE_DOC, claim)
        message = str(exc.value)
        assert "NOT verified" in message
        # The leak into the borrower field is named so the false-verified case is unmistakable.
        assert "Borrower" in message

    def test_term_in_expected_security_row_is_verified(self) -> None:
        claim = ColumnClaim(term="Leasehold", column_label="Type")
        # No raise == verified.
        check_doc_evidence(_TRUE_POSITIVE_DOC, claim)


class TestFieldClaim:
    def test_field_contains_true_when_field_holds_term(self) -> None:
        doc = StructuredDoc(fields={"Security Type": "Leasehold"})
        assert field_contains(doc, "Security Type", "Leasehold") is True

    def test_field_contains_false_when_field_lacks_term(self) -> None:
        doc = StructuredDoc(fields={"Security Type": "Mortgage"})
        assert field_contains(doc, "Security Type", "Leasehold") is False

    def test_field_claim_verified_when_named_field_holds_term(self) -> None:
        doc = StructuredDoc(fields={"Security Type": "Leasehold", "Borrower": "Jane Doe"})
        check_doc_evidence(doc, FieldClaim(term="Leasehold", field_label="Security Type"))

    def test_field_claim_not_verified_when_term_only_in_other_field(self) -> None:
        doc = StructuredDoc(fields={"Security Type": "Mortgage", "Borrower": "E2E Leasehold"})
        with pytest.raises(DocEvidenceError) as exc:
            check_doc_evidence(doc, FieldClaim(term="Leasehold", field_label="Security Type"))
        assert "Borrower" in str(exc.value)


class TestMissingAnchorFailsLoud:
    """When the named anchor does not exist, the probe fails loud — never skip-as-pass."""

    def test_missing_field_raises_not_returns_false(self) -> None:
        doc = StructuredDoc(fields={"Borrower": "E2E Leasehold"})
        with pytest.raises(DocEvidenceError) as exc:
            field_contains(doc, "Security Type", "Leasehold")
        message = str(exc.value)
        assert "no such field" in message.lower()
        # The incidental leak is surfaced even on the fail-loud path.
        assert "Borrower" in message

    def test_missing_column_raises_not_returns_false(self) -> None:
        doc = StructuredDoc(fields={"Borrower": "E2E Leasehold"}, rows=[{"Value": "100000"}])
        with pytest.raises(DocEvidenceError) as exc:
            row_cell_contains(doc, "Type", "Leasehold")
        assert "no row carries that column" in str(exc.value).lower()

    def test_missing_anchor_through_check_doc_evidence_fails_loud(self) -> None:
        doc = StructuredDoc(fields={"Borrower": "E2E Leasehold"})
        with pytest.raises(DocEvidenceError):
            check_doc_evidence(doc, ColumnClaim(term="Leasehold", column_label="Type"))


class TestRowCellContains:
    def test_finds_term_in_any_row(self) -> None:
        doc = StructuredDoc(rows=[{"Type": "Mortgage"}, {"Type": "Leasehold"}])
        assert row_cell_contains(doc, "Type", "Leasehold") is True

    def test_false_when_no_row_cell_holds_term(self) -> None:
        doc = StructuredDoc(rows=[{"Type": "Mortgage"}, {"Type": "Pfand"}])
        assert row_cell_contains(doc, "Type", "Leasehold") is False

    def test_tolerates_rows_missing_the_column(self) -> None:
        doc = StructuredDoc(rows=[{"Type": "Leasehold"}, {"Value": "100000"}])
        assert row_cell_contains(doc, "Type", "Leasehold") is True


class TestNormalization:
    def test_match_is_case_insensitive(self) -> None:
        doc = StructuredDoc(fields={"Security Type": "LEASEHOLD"})
        assert field_contains(doc, "Security Type", "leasehold") is True

    def test_match_collapses_whitespace(self) -> None:
        doc = StructuredDoc(fields={"Security Type": "Ground  Lease"})
        assert field_contains(doc, "Security Type", "Ground Lease") is True


class TestStructuredDocIntrospection:
    def test_column_names_dedupe_across_rows(self) -> None:
        doc = StructuredDoc(rows=[{"Type": "a", "Value": "1"}, {"Type": "b"}])
        assert doc.column_names() == ["Type", "Value"]

    def test_field_names_lists_fields(self) -> None:
        doc = StructuredDoc(fields={"A": "1", "B": "2"})
        assert doc.field_names() == ["A", "B"]

    def test_free_text_fields_containing_lists_incidental_hits(self) -> None:
        doc = StructuredDoc(fields={"Borrower": "E2E Leasehold", "Amount": "100000"})
        assert doc.free_text_fields_containing("Leasehold") == ["Borrower"]
