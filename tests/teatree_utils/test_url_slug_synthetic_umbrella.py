"""The synthetic-loop umbrella anchor predicate (souliane/teatree#3706).

Every synthetic loop ticket — directive interpret/implement, outer-loop experiment —
anchors on ONE umbrella issue and disambiguates via a URL fragment. The predicate
recognises that anchor (any fragment, or none) so the artifact-terminal task sweep can
skip these FSM-owned tasks regardless of their phase.
"""

from teatree.utils.url_slug import SYNTHETIC_LOOP_UMBRELLA_URL, is_synthetic_loop_umbrella_url


class TestIsSyntheticLoopUmbrellaUrl:
    def test_bare_umbrella_matches(self) -> None:
        assert is_synthetic_loop_umbrella_url(SYNTHETIC_LOOP_UMBRELLA_URL)

    def test_interpret_fragment_matches(self) -> None:
        assert is_synthetic_loop_umbrella_url(f"{SYNTHETIC_LOOP_UMBRELLA_URL}#directive=5")

    def test_implement_fragment_matches(self) -> None:
        assert is_synthetic_loop_umbrella_url(f"{SYNTHETIC_LOOP_UMBRELLA_URL}#directive-impl=5")

    def test_outer_loop_fragment_matches(self) -> None:
        assert is_synthetic_loop_umbrella_url(f"{SYNTHETIC_LOOP_UMBRELLA_URL}#outer-loop-experiment=7")

    def test_a_real_issue_does_not_match(self) -> None:
        assert not is_synthetic_loop_umbrella_url("https://github.com/souliane/teatree/issues/42")

    def test_a_numeric_superstring_of_the_umbrella_does_not_match(self) -> None:
        # #30091 startswith #3009 textually — the base must be an EXACT match, not a prefix.
        assert not is_synthetic_loop_umbrella_url("https://github.com/souliane/teatree/issues/30091")

    def test_empty_url_does_not_match(self) -> None:
        assert not is_synthetic_loop_umbrella_url("")
