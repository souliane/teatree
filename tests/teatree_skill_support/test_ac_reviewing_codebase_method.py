# test-path: cross-cutting — a skills/ ↔ quality-catalog contract; the subject is the
# skill prose, and the catalog is only the authority it is checked against.
"""The periodic holistic review's METHOD is stated in its skill, not reinvented per pass.

Every assertion here is one acceptance criterion of
`souliane/teatree#4215 <https://github.com/souliane/teatree/issues/4215>`_: a
verification step that defaults to refuted, a coverage statement that makes a
clean verdict auditable, the three defect classes this repo keeps hitting, the
standing shape checklist, the three method notes, and the bounded-coverage
disclosure.

``test_every_judgement_entry_is_named`` is the anti-drift one. The skill's
enumeration of judgement-tier entries is a derived cache of
``src/teatree/quality/antipatterns.yaml``; it was already stale by five entries
when this test was written, which is the exact "same fact in two co-equal
stores" shape the pass is supposed to find.
"""

import re
from pathlib import Path

from teatree.quality.catalog import load_catalog

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_PATH = _REPO_ROOT / "skills" / "ac-reviewing-codebase" / "SKILL.md"

#: Catalog ids the skill must carry as the standing, re-checked-every-pass subset.
_STANDING_SHAPE_IDS = (
    "unknown-reported-as-verdict",
    "vacuous-guard",
    "shipped-inert",
    "gate-fails-open-on-error",
    "silent-success-on-failure",
    "silent-freeze",
    "long-io-holds-control-lock",
    "destructive-op-outside-its-guard",
    "self-declared-identity-authorization",
    "silent-truncation-pagination",
)


def _body() -> str:
    return _SKILL_PATH.read_text(encoding="utf-8")


class TestVerificationStep:
    def test_findings_start_refuted(self) -> None:
        body = _body().lower()
        assert "refuted" in body, "no verification step: findings must default to refuted"
        assert "positively confirm" in body, "the refuted default needs a positive-confirmation rule to be falsifiable"

    def test_verifier_is_a_distinct_reader(self) -> None:
        assert "distinct reviewer" in _body(), "verification must be a different reader, not the finder re-reading"

    def test_severity_is_downgradable_at_verification(self) -> None:
        body = _body().lower()
        assert "downgrad" in body, "severity must be downgradable at the verification step"


class TestCoverageStatement:
    def test_every_subsystem_verdict_states_what_was_checked(self) -> None:
        body = _body().lower()
        assert "what you checked" in body, "a subsystem verdict must carry an explicit what-I-checked statement"
        assert "unfalsifiable" in body, "the reason a bare verdict is worthless must be stated"

    def test_bounded_coverage_is_disclosed(self) -> None:
        body = _body().lower()
        assert "did not cover" in body, "a pass that bounds its own coverage must say what it left out"


class TestDefectClasses:
    def test_the_three_classes_are_named(self) -> None:
        body = _body()
        for entry_id in ("unknown-reported-as-verdict", "vacuous-guard", "shipped-inert"):
            assert entry_id in body, f"defect class not named as a thing to hunt: {entry_id}"

    def test_the_vacuous_guard_mutation_test_is_spelled_out(self) -> None:
        body = _body().lower()
        assert "name the mutation" in body, "the vacuous-guard class needs its mutation test spelled out"


class TestStandingChecklist:
    def test_every_standing_shape_is_carried(self) -> None:
        body = _body()
        missing = [entry_id for entry_id in _STANDING_SHAPE_IDS if entry_id not in body]
        assert missing == [], f"standing checklist missing catalog ids: {missing}"

    def test_standing_shapes_resolve_to_real_catalog_entries(self) -> None:
        known = {entry.id for entry in load_catalog()}
        missing = [entry_id for entry_id in _STANDING_SHAPE_IDS if entry_id not in known]
        assert missing == [], f"standing checklist names ids absent from antipatterns.yaml: {missing}"

    def test_every_judgement_entry_is_named(self) -> None:
        body = _body()
        judgement = (entry for entry in load_catalog() if entry.detection == "judgement")
        missing = sorted(entry.name for entry in judgement if entry.name not in body)
        assert missing == [], f"skill's judgement-entry list is stale — missing: {missing}"


class TestSectionCrossReferences:
    """Catches a `§ N` pointing at no section.

    It cannot catch one aimed at the WRONG existing section — inserting § 2 and § 3
    silently re-aimed six live refs, and only a read found the one left behind.

    A dotted `§ 17.4` is BLUEPRINT's numbering, not this skill's, so it is not a ref
    this file can resolve.
    """

    def test_no_reference_dangles(self) -> None:
        body = _body()
        numbered = {int(m) for m in re.findall(r"^### (\d+)\.", body, flags=re.MULTILINE)}
        referenced = {int(m) for m in re.findall(r"§\s*(\d+)\b(?!\.\d)", body)}
        assert referenced <= numbered, f"§ refs pointing at no section: {sorted(referenced - numbered)}"


class TestMethodNotes:
    def test_paginated_reads(self) -> None:
        assert "Read paginated" in _body(), "the paginate note is missing"

    def test_parallel_subsystem_readers(self) -> None:
        body = _body().lower()
        assert "parallel subsystem readers" in body, "the parallel-readers note is missing"

    def test_probe_venue_is_confirmed(self) -> None:
        body = _body().lower()
        assert "probe from the place that matters" in body, "the probe-venue note is missing"
