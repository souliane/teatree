"""Pure plan-adequacy validators (SELFCATCH-3) — the structural anti-thin-spec check.

Silence never passes: a section is complete only when substantive OR carrying an
explicit reasoned negative. A scope+acceptance thin spec (no seams/edge-cases/
test-strategy claims) fails ``is_adequate`` — the named root cause of the 26-bug
integration campaign made structurally impossible at the manifest level.
"""

from teatree.core.models.plan_adequacy import (
    REQUIRED_ADEQUACY_SECTIONS,
    all_negated_adequacy,
    declared_seam_paths,
    is_adequate,
    is_valid_base_sha,
    negated_section,
    section_complete,
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
