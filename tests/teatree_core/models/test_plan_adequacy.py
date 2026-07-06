"""Pure plan-adequacy validators (SELFCATCH-3) — the structural anti-thin-spec check.

Silence never passes: a section is complete only when substantive OR carrying an
explicit reasoned negative. A scope+acceptance thin spec (no seams/edge-cases/
test-strategy claims) fails ``is_adequate`` — the named root cause of the 26-bug
integration campaign made structurally impossible at the manifest level.
"""

from types import SimpleNamespace

from teatree.core.models.mechanism_sketch import MechanismSketch
from teatree.core.models.plan_adequacy import (
    DIRECTIVE_ADEQUACY_SECTIONS,
    REQUIRED_ADEQUACY_SECTIONS,
    _is_plan_bypass_shaped,
    all_negated_adequacy,
    declared_seam_paths,
    is_adequate,
    is_valid_base_sha,
    mechanism_conforms,
    negated_section,
    required_sections_for,
    section_complete,
    sections_adequate,
)


class TestIsValidBaseSha:
    def test_full_40_char_hex_is_valid(self) -> None:
        assert is_valid_base_sha("a" * 40) is True
        assert is_valid_base_sha("0123456789abcdef0123456789ABCDEF01234567") is True

    def test_short_or_non_hex_is_invalid(self) -> None:
        assert is_valid_base_sha("abc123") is False
        assert is_valid_base_sha("g" * 40) is False  # non-hex
        assert is_valid_base_sha("a" * 41) is False

    def test_non_string_and_blank_are_invalid(self) -> None:
        assert is_valid_base_sha("") is False
        assert is_valid_base_sha(None) is False
        assert is_valid_base_sha(40) is False

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert is_valid_base_sha(f"  {'a' * 40}  ") is True


class TestSectionComplete:
    def test_substantive_text_is_complete(self) -> None:
        assert section_complete({"content": "a real design note"}) is True

    def test_substantive_list_is_complete(self) -> None:
        assert section_complete({"content": ["src/a.py", "src/b.py"]}) is True

    def test_explicit_negative_is_complete(self) -> None:
        assert section_complete({"none_reason": "pure refactor, no new seams"}) is True

    def test_silence_never_passes(self) -> None:
        assert section_complete({}) is False
        assert section_complete({"content": ""}) is False
        assert section_complete({"content": []}) is False
        assert section_complete({"content": ["  "]}) is False
        assert section_complete({"none_reason": "   "}) is False

    def test_non_dict_is_incomplete(self) -> None:
        assert section_complete("just a string") is False
        assert section_complete(None) is False


class TestIsAdequate:
    def _full(self) -> dict:
        return {
            "design": {"content": "split X into two methods"},
            "integration_seams": {"content": ["src/teatree/core/gates/plan_currency_gate.py"]},
            "edge_cases": {"content": ["stale base", "offline fetch"]},
            "test_strategy": {"content": "red-first on stale-base refusal"},
        }

    def test_complete_four_section_manifest_is_adequate(self) -> None:
        assert is_adequate(self._full()) is True

    def test_thin_scope_acceptance_spec_is_inadequate(self) -> None:
        # A scope+acceptance-only spec — no seams/edge-cases/test-strategy claims.
        assert is_adequate({}) is False
        assert is_adequate({"design": {"content": "do the thing"}}) is False

    def test_a_single_silent_section_fails(self) -> None:
        manifest = self._full()
        manifest["edge_cases"] = {}
        assert is_adequate(manifest) is False

    def test_all_explicit_negatives_is_adequate(self) -> None:
        assert is_adequate(all_negated_adequacy("trivial mechanical change")) is True

    def test_non_dict_is_inadequate(self) -> None:
        assert is_adequate(None) is False
        assert is_adequate("scope: x\nacceptance: y") is False

    def test_required_sections_are_the_four(self) -> None:
        assert set(REQUIRED_ADEQUACY_SECTIONS) == {"design", "integration_seams", "edge_cases", "test_strategy"}


class TestDeclaredSeamPaths:
    def test_reads_the_integration_seams_content_list(self) -> None:
        adequacy = {"integration_seams": {"content": ["a/b.py", "c/d.py"]}}
        assert declared_seam_paths(adequacy) == ("a/b.py", "c/d.py")

    def test_a_single_string_seam_is_wrapped(self) -> None:
        assert declared_seam_paths({"integration_seams": {"content": "a/b.py"}}) == ("a/b.py",)

    def test_no_seams_negative_yields_empty(self) -> None:
        assert declared_seam_paths(negated_section_manifest()) == ()

    def test_missing_or_malformed_yields_empty(self) -> None:
        assert declared_seam_paths({}) == ()
        assert declared_seam_paths(None) == ()
        assert declared_seam_paths({"integration_seams": "oops"}) == ()

    def test_blank_entries_are_dropped(self) -> None:
        assert declared_seam_paths({"integration_seams": {"content": ["a.py", "  ", ""]}}) == ("a.py",)


def negated_section_manifest() -> dict:
    return {"integration_seams": negated_section("pure refactor")}


# --------------------------------------------------------------------------- #
# north-star PR-5 — mechanism_placement (the directive-scoped 5th adequacy section).
# --------------------------------------------------------------------------- #
_CORE_CHOKEPOINT = "src/teatree/core/gates/pr_budget_gate.py::check_pr_budget"


def _sketch(**overrides: object) -> MechanismSketch:
    base = {
        "kind": "setting_policy_gate",
        "setting_key": "max_open_prs_per_repo_per_ticket",
        "setting_type": "int",
        "neutral_default": 0,
        "policy_chokepoint": _CORE_CHOKEPOINT,
        "activation_scope": "example-overlay",
        "activation_value": 1,
        "rejected_alternatives": ("an overlay-local hook — a second overlay wanting max 2 needs new code",),
    }
    base.update(overrides)
    return MechanismSketch(**base)


def _conforming_section(**overrides: object) -> dict:
    section = {
        "setting_key": "max_open_prs_per_repo_per_ticket",
        "neutral_default": 0,
        "policy_chokepoint": _CORE_CHOKEPOINT,
        "activation_scope": "example-overlay",
        "activation_value": 1,
        "rejected_alternatives": ["an overlay-local hook — a second overlay wanting max 2 needs new code"],
    }
    section.update(overrides)
    return section


def _directive_manifest(section: dict | None) -> dict:
    manifest = {
        "design": {"content": "add the setting + policy gate at the core seam"},
        "integration_seams": {"content": [_CORE_CHOKEPOINT.split("::", maxsplit=1)[0]]},
        "edge_cases": {"content": ["neutral default keeps core inert"]},
        "test_strategy": {"content": "red-first refusal on the budget count"},
    }
    if section is not None:
        manifest["mechanism_placement"] = section
    return manifest


class TestSectionsAdequateAndRequiredSections:
    def test_sections_adequate_generalises_is_adequate_over_a_section_tuple(self) -> None:
        manifest = dict(all_negated_adequacy("clean"))
        assert sections_adequate(manifest, REQUIRED_ADEQUACY_SECTIONS) is True
        assert sections_adequate(manifest, ("mechanism_placement",)) is False  # section absent

    def test_directive_sections_are_the_four_plus_mechanism_placement(self) -> None:
        assert (*REQUIRED_ADEQUACY_SECTIONS, "mechanism_placement") == DIRECTIVE_ADEQUACY_SECTIONS

    def test_required_sections_for_a_directive_ticket_is_five(self) -> None:
        directive_ticket = SimpleNamespace(extra={"directive_id": 7})
        assert required_sections_for(directive_ticket) == DIRECTIVE_ADEQUACY_SECTIONS

    def test_required_sections_for_an_ordinary_ticket_is_four(self) -> None:
        assert required_sections_for(SimpleNamespace(extra={})) == REQUIRED_ADEQUACY_SECTIONS
        assert required_sections_for(object()) == REQUIRED_ADEQUACY_SECTIONS


class TestMechanismConforms:
    def test_a_conforming_placement_passes(self) -> None:
        assert mechanism_conforms(_directive_manifest(_conforming_section()), _sketch()) is None

    def test_an_overlay_package_chokepoint_is_a_hack_and_fails(self) -> None:
        # Anti-vacuity (a): a chokepoint under an overlay package — the one-off hack — FAILS.
        section = _conforming_section(policy_chokepoint="src/teatree/overlays/acme/hooks.py::cap_prs")
        finding = mechanism_conforms(_directive_manifest(section), _sketch())
        assert finding is not None
        assert "not a core seam" in finding

    def test_a_contrib_chokepoint_is_a_hack_and_fails(self) -> None:
        section = _conforming_section(policy_chokepoint="contrib/acme/patch.py::cap")
        assert "not a core seam" in (mechanism_conforms(_directive_manifest(section), _sketch()) or "")

    def test_a_missing_section_fails(self) -> None:
        finding = mechanism_conforms(_directive_manifest(None), _sketch())
        assert finding is not None
        assert "no mechanism_placement section" in finding

    def test_a_drifted_chokepoint_fails(self) -> None:
        section = _conforming_section(policy_chokepoint="src/teatree/core/runners/ship.py::run")
        assert "drifts" in (mechanism_conforms(_directive_manifest(section), _sketch()) or "")

    def test_a_drifted_setting_key_fails(self) -> None:
        section = _conforming_section(setting_key="some_other_setting")
        assert "setting_key" in (mechanism_conforms(_directive_manifest(section), _sketch()) or "")

    def test_a_drifted_neutral_default_fails(self) -> None:
        section = _conforming_section(neutral_default=1)  # 1 is not the neutral/inert default
        assert "neutral_default" in (mechanism_conforms(_directive_manifest(section), _sketch()) or "")

    def test_a_drifted_activation_scope_or_value_fails(self) -> None:
        assert "activation_scope" in (
            mechanism_conforms(_directive_manifest(_conforming_section(activation_scope="other")), _sketch()) or ""
        )
        assert "activation_value" in (
            mechanism_conforms(_directive_manifest(_conforming_section(activation_value=2)), _sketch()) or ""
        )

    def test_empty_rejected_alternatives_fails_the_n2_litmus(self) -> None:
        section = _conforming_section(rejected_alternatives=[])
        finding = mechanism_conforms(_directive_manifest(section), _sketch())
        assert finding is not None
        assert "N=2 litmus" in finding

    def test_omitting_a_sketch_required_refactor_fails(self) -> None:
        sketch = _sketch(refactors=("consolidate the two host.create_pr call sites",))
        section = _conforming_section(refactors=[])  # the sketch requires a refactor the plan omits
        finding = mechanism_conforms(_directive_manifest(section), sketch)
        assert finding is not None
        assert "refactors" in finding

    def test_declaring_the_sketch_refactor_passes(self) -> None:
        refactor = "consolidate the two host.create_pr call sites"
        sketch = _sketch(refactors=(refactor,))
        section = _conforming_section(refactors=[refactor])
        assert mechanism_conforms(_directive_manifest(section), sketch) is None


class TestNeverLockoutEscapes:
    def test_a_section_reasoned_negative_waives(self) -> None:
        # (d) never-lockout: an explicit reasoned-negative section waives the structured check.
        section = {"none_reason": "genuinely mechanism-less directive — configuration only"}
        assert mechanism_conforms(_directive_manifest(section), _sketch()) is None

    def test_a_plan_bypass_shaped_manifest_waives(self) -> None:
        # (d) never-lockout: the audited plan-bypass manifest (all reasoned negatives) waives too.
        bypass = dict(all_negated_adequacy("audited plan-bypass"))
        assert _is_plan_bypass_shaped(bypass) is True
        assert mechanism_conforms(bypass, _sketch()) is None

    def test_a_normal_directive_manifest_is_not_bypass_shaped(self) -> None:
        assert _is_plan_bypass_shaped(_directive_manifest(_conforming_section())) is False
