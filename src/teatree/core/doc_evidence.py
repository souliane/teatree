"""Field-context evidence probe for generated-doc / export verification (#2296).

The recurrence this forecloses: a generated document or export (PDF /
spreadsheet / rendered report) was declared "verified" against an acceptance
criterion because the constrained term appeared *somewhere* on the page — but
the only match was an incidental free-text field (a borrower NAME like
"E2E Leasehold"), not the structured row the AC actually constrains (a
security / guarantee-type row). A page-wide ``if term in page_text`` check
passed; the structured fact the AC required was absent, and the user was handed
a false "verified" PDF.

A remembered rule ("verify by field context, not a page-wide substring") did
not hold under load; this module is the deterministic substitute, mirroring the
:mod:`teatree.core.test_plan_validation` shape — pure string logic over a parsed
document, a dedicated error subclass, and a clear message naming exactly what is
missing — so it unit-tests in isolation with no ORM, host, or network.

The probe binds the assertion to the **structured location** the AC constrains:

Structured document
    A generated doc is modelled as :class:`StructuredDoc` — a map of named
    ``fields`` (label → value, e.g. ``"Borrower" -> "E2E Leasehold"``) and a list
    of table ``rows`` (each a label → cell-value map, e.g. a Security row
    ``{"Type": "Leasehold", "Amount": "100000"}``). This is what a structured
    extractor (a PDF form-field reader, a spreadsheet parser, a rendered-report
    DOM walk) yields — NOT a flattened blob of page text.

Field-context assertion (the evidence)
    Evidence asserts a term in a *named anchor*: a specific field
    (:func:`field_contains`) or a specific table column within rows
    (:func:`row_cell_contains`). The term must be found in THAT anchor's value,
    not anywhere else in the document. A match in an unrelated field (the
    borrower name) does NOT satisfy an assertion that constrains the Security
    row.

Fail-loud, never skip-as-pass
    When the named anchor does not exist in the document at all (no such field,
    no such column, an empty row set), the probe raises
    :class:`DocEvidenceError` — it CANNOT verify, so it fails loud rather than
    falling back to an incidental free-text match. A page-wide substring is
    never accepted as evidence; :func:`reject_page_wide_substring` exists to make
    that rejection explicit and testable.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field


class DocEvidenceError(ValueError):
    """A generated-doc / export evidence assertion could not be verified.

    Raised when the named structured anchor (field or table column) the AC
    constrains is absent from the document, or when a page-wide substring is
    offered as evidence. The message names the missing anchor (and, where the
    term DOES appear, the unrelated field it leaked into) so the agent can
    correct the probe rather than accept a false "verified".
    """


def _norm(text: str) -> str:
    """Casefold + collapse surrounding whitespace for tolerant term matching."""
    return " ".join(text.split()).casefold()


def _contains(haystack: str, term: str) -> bool:
    """True when *term* appears in *haystack* under normalized (casefold) matching."""
    return _norm(term) in _norm(haystack)


@dataclass(frozen=True, slots=True)
class StructuredDoc:
    """A generated doc parsed into named fields and labelled table rows.

    ``fields`` maps a field label to its single value (form fields, header
    key/value pairs). ``rows`` is a list of table rows, each mapping a column
    label to its cell value. Both come from a STRUCTURED extractor — a PDF
    form-field reader, a spreadsheet parser, a rendered-report DOM walk — never
    from a flattened page-text blob, so an assertion can be bound to the exact
    field/column the AC constrains.
    """

    fields: Mapping[str, str] = field(default_factory=dict)
    rows: Sequence[Mapping[str, str]] = field(default_factory=tuple)

    def field_names(self) -> list[str]:
        return list(self.fields)

    def column_names(self) -> list[str]:
        seen: dict[str, None] = {}
        for row in self.rows:
            for column in row:
                seen.setdefault(column, None)
        return list(seen)

    def free_text_fields_containing(self, term: str) -> list[str]:
        """Names of the fields whose value contains *term* (the incidental hits)."""
        return [name for name, value in self.fields.items() if _contains(value, term)]


def field_contains(doc: StructuredDoc, field_label: str, term: str) -> bool:
    """True iff *term* appears in the value of the named *field_label*.

    Fails loud (:class:`DocEvidenceError`) when the document has no field with
    that label — the assertion cannot be verified, so it must NOT silently fall
    back to a page-wide match. When the field exists but does not contain the
    term, returns ``False`` (the structured fact the AC requires is absent).
    """
    if field_label not in doc.fields:
        leaked = doc.free_text_fields_containing(term)
        leak_note = (
            f" (the term DOES appear in unrelated field(s) {leaked!r} — an "
            f"incidental free-text match, not the constrained field)"
            if leaked
            else ""
        )
        msg = (
            f"Cannot verify {term!r} in field {field_label!r}: the document has no "
            f"such field. Known fields: {doc.field_names()!r}.{leak_note} Bind the "
            f"assertion to the field the AC constrains, or fail loud — never accept a "
            f"page-wide substring."
        )
        raise DocEvidenceError(msg)
    return _contains(doc.fields[field_label], term)


def row_cell_contains(doc: StructuredDoc, column_label: str, term: str) -> bool:
    """True iff *term* appears in the *column_label* cell of any table row.

    Fails loud (:class:`DocEvidenceError`) when no row carries that column — the
    structured row the AC constrains does not exist, so the probe cannot verify
    and must not fall back to a page-wide match. When the column exists but no
    cell contains the term, returns ``False``.
    """
    if column_label not in doc.column_names():
        leaked = doc.free_text_fields_containing(term)
        leak_note = (
            f" (the term DOES appear in unrelated field(s) {leaked!r} — an "
            f"incidental free-text match, not the constrained row)"
            if leaked
            else ""
        )
        msg = (
            f"Cannot verify {term!r} in table column {column_label!r}: no row carries "
            f"that column. Known columns: {doc.column_names()!r}.{leak_note} Bind the "
            f"assertion to the row the AC constrains, or fail loud — never accept a "
            f"page-wide substring."
        )
        raise DocEvidenceError(msg)
    return any(_contains(row[column_label], term) for row in doc.rows if column_label in row)


@dataclass(frozen=True, slots=True)
class FieldClaim:
    """An AC-scoped claim that *term* must appear in the named *field_label*."""

    term: str
    field_label: str

    def anchor(self) -> str:
        return f"field {self.field_label!r}"

    def verify(self, doc: StructuredDoc) -> bool:
        return field_contains(doc, self.field_label, self.term)


@dataclass(frozen=True, slots=True)
class ColumnClaim:
    """An AC-scoped claim that *term* must appear in the named *column_label* cell."""

    term: str
    column_label: str

    def anchor(self) -> str:
        return f"table column {self.column_label!r}"

    def verify(self, doc: StructuredDoc) -> bool:
        return row_cell_contains(doc, self.column_label, self.term)


# A field-context claim names exactly one structured anchor. The two purpose-typed
# claim classes make "field XOR column" a type-level guarantee — there is no
# both-set / neither-set state to defensively guard against at verification time.
FieldEvidenceClaim = FieldClaim | ColumnClaim


def check_doc_evidence(doc: StructuredDoc, claim: FieldEvidenceClaim) -> None:
    """Verify *claim* against *doc*, raising :class:`DocEvidenceError` on a miss.

    Passes silently when the term is present in the claim's named anchor (field
    or table column). Raises when the anchor is absent (cannot verify — fail
    loud) OR when the anchor exists but does not carry the term (the structured
    fact the AC requires is absent — the false-positive the page-wide substring
    would have masked). The message names the unrelated field(s) the term leaked
    into, when any, so the false-verified case is unmistakable.
    """
    if claim.verify(doc):
        return
    leaked = doc.free_text_fields_containing(claim.term)
    leak_note = (
        f" The term appears only in unrelated free-text field(s) {leaked!r} — an "
        f"incidental match, NOT evidence the constrained location holds the value."
        if leaked
        else ""
    )
    msg = (
        f"NOT verified: {claim.term!r} is absent from {claim.anchor()}.{leak_note} A "
        f"page-wide substring match does not satisfy this acceptance criterion."
    )
    raise DocEvidenceError(msg)


def reject_page_wide_substring(page_text: str, term: str) -> None:
    """Always refuse a page-wide / document-wide substring as evidence.

    A bare ``term in page_text`` over generated-doc output is NOT evidence: the
    match may be an incidental free-text occurrence (a borrower name) rather than
    the structured field/row the AC constrains. This helper exists to make that
    rejection explicit and callable — it raises :class:`DocEvidenceError`
    unconditionally (whether or not the term is present), directing the caller to
    :func:`check_doc_evidence` with a named anchor instead.
    """
    matched = _contains(page_text, term)
    detail = (
        "the term is present somewhere on the page, but a page-wide match cannot "
        "tell the constrained field/row from an incidental free-text hit"
        if matched
        else "the term is not present, and a page-wide scan is the wrong probe regardless"
    )
    msg = (
        f"Refusing page-wide substring as evidence for {term!r}: {detail}. Use "
        f"check_doc_evidence() with the field/column the AC constrains."
    )
    raise DocEvidenceError(msg)
