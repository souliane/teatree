"""Every shipped conformance invariant carries a populated ``catalog_ref`` (#166).

The ``Invariant.catalog_ref`` field was wired but every invariant set it to
``None``. WI-7b populates each invariant with a clickable reference to the
canonical rule it enforces (the rules-skill section), closing the #166 catalog
linkage. These tests pin that every shipped invariant (GREEN ship-blocking and
AMBER audit-only alike) carries a non-``None`` ref AND that the serialized JSON
report surfaces it — so reverting any ``catalog_ref`` back to ``None`` goes RED.
"""

import json

from teatree.eval.session_transcript import parse_session_jsonl
from teatree.eval.transcript_conformance import (
    AUDIT_REGISTRY,
    INVARIANT_REGISTRY,
    Invariant,
    render_report_json,
    replay,
)

_CLEAN = '{"type":"assistant","message":{"role":"assistant","content":[]}}\n'


def _all_shipped() -> tuple[Invariant, ...]:
    """Every invariant the project ships — the GREEN + AMBER union, deduped by id."""
    seen: dict[str, Invariant] = {}
    for invariant in (*INVARIANT_REGISTRY, *AUDIT_REGISTRY):
        seen.setdefault(invariant.id, invariant)
    return tuple(seen.values())


def test_every_shipped_invariant_has_a_catalog_ref() -> None:
    missing = [inv.id for inv in _all_shipped() if inv.catalog_ref is None]
    assert not missing, (
        "shipped conformance invariant(s) carry no catalog_ref — the #166 catalog "
        f"linkage is unset for: {missing}. Populate each with the rule it enforces."
    )


def test_catalog_refs_are_clickable_rule_links() -> None:
    """Each ref points at the souliane/teatree rules-skill source (a clickable link)."""
    for inv in _all_shipped():
        ref = inv.catalog_ref
        assert ref is not None, f"{inv.id} catalog_ref is None"
        assert ref.startswith("https://github.com/souliane/teatree/"), (
            f"{inv.id} catalog_ref must be a clickable souliane/teatree link, got {ref!r}"
        )
        assert "skills/rules/SKILL.md" in ref, f"{inv.id} catalog_ref must cite the rules-skill source, got {ref!r}"


def test_catalog_refs_are_distinct_enough_to_disambiguate() -> None:
    """A ref names a specific section anchor (not a bare file link), so it disambiguates."""
    for inv in _all_shipped():
        ref = inv.catalog_ref
        assert ref is not None, f"{inv.id} catalog_ref is None"
        assert "#" in ref, (
            f"{inv.id} catalog_ref must anchor a specific rule section (carry a '#fragment'), got {ref!r}"
        )


def test_json_report_surfaces_catalog_ref_for_green_invariants() -> None:
    """The serialized report emits each GREEN invariant's populated catalog_ref."""
    results = replay(parse_session_jsonl(_CLEAN))
    payload = json.loads(render_report_json(results))
    by_id = {row["id"]: row for row in payload["invariants"]}
    for inv in INVARIANT_REGISTRY:
        assert by_id[inv.id]["catalog_ref"] == inv.catalog_ref
        assert by_id[inv.id]["catalog_ref"] is not None, f"JSON report dropped a populated catalog_ref for {inv.id}"


def test_json_report_surfaces_catalog_ref_for_audit_registry() -> None:
    """The audit superset's serialized report carries each invariant's catalog_ref too."""
    results = replay(parse_session_jsonl(_CLEAN), AUDIT_REGISTRY)
    payload = json.loads(render_report_json(results, AUDIT_REGISTRY))
    by_id = {row["id"]: row for row in payload["invariants"]}
    for inv in AUDIT_REGISTRY:
        assert by_id[inv.id]["catalog_ref"] == inv.catalog_ref
        assert by_id[inv.id]["catalog_ref"] is not None
