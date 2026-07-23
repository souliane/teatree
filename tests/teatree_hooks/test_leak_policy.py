"""The one leak-policy decision function is total and preserves today's verdicts (#3532).

Eleven enforcement points used to each compose their own (term source x matcher x
visibility resolver x fail direction) tuple. :mod:`teatree.hooks.leak_policy` is the
single home for the ``(term class, destination visibility, surface) -> verdict``
decision they now share. These tests pin the matrix so a future edit to one call
site cannot quietly re-fork the policy.

All terms here are SYNTHETIC class names, not configured values — nothing leaks.
"""

import json

import pytest

from teatree.hooks import banned_term_registry, leak_policy
from teatree.hooks.leak_policy import Surface, Verdict, Visibility, scans_on_visibility
from teatree.hooks.public_visibility import destination_visibility
from teatree.hooks.publish_destination import Destination

_BLOCKING_CLASSES = (banned_term_registry.LEAK, banned_term_registry.PROSE_COLLIDER)


class TestDecideIsTotal:
    """``decide`` answers for every (class, visibility, surface) triple."""

    @pytest.mark.parametrize("term_class", banned_term_registry.TERM_CLASSES)
    @pytest.mark.parametrize("visibility", list(Visibility))
    @pytest.mark.parametrize("surface", list(Surface))
    def test_every_triple_has_a_verdict(self, term_class: str, visibility: Visibility, surface: Surface) -> None:
        assert leak_policy.decide(term_class, visibility, surface) in set(Verdict)

    def test_unknown_class_raises_rather_than_allowing(self) -> None:
        with pytest.raises(ValueError, match="unknown term class"):
            leak_policy.decide("not-a-class", Visibility.PUBLIC, Surface.DIFF)


class TestVisibilityPolarity:
    """UNKNOWN visibility is treated as PUBLIC — the fail-closed direction."""

    @pytest.mark.parametrize("term_class", _BLOCKING_CLASSES)
    def test_public_and_unknown_both_block(self, term_class: str) -> None:
        assert leak_policy.decide(term_class, Visibility.PUBLIC, Surface.DIFF) is Verdict.BLOCK
        assert leak_policy.decide(term_class, Visibility.UNKNOWN, Surface.DIFF) is Verdict.BLOCK

    @pytest.mark.parametrize("term_class", banned_term_registry.TERM_CLASSES)
    @pytest.mark.parametrize("surface", list(Surface))
    def test_non_public_never_blocks(self, term_class: str, surface: Surface) -> None:
        assert leak_policy.decide(term_class, Visibility.NON_PUBLIC, surface) is Verdict.ALLOW

    @pytest.mark.parametrize("visibility", list(Visibility))
    @pytest.mark.parametrize("surface", list(Surface))
    def test_allow_class_never_blocks(self, visibility: Visibility, surface: Surface) -> None:
        assert leak_policy.decide(banned_term_registry.ALLOW, visibility, surface) is Verdict.ALLOW


class TestSurfaceScope:
    """A class a surface does not scan resolves ALLOW there, not BLOCK."""

    def test_tree_scans_only_the_leak_class(self) -> None:
        assert leak_policy.decide(banned_term_registry.LEAK, Visibility.PUBLIC, Surface.TREE) is Verdict.BLOCK
        assert leak_policy.decide(banned_term_registry.TONE, Visibility.PUBLIC, Surface.TREE) is Verdict.ALLOW

    def test_core_does_not_scan_tone(self) -> None:
        assert leak_policy.decide(banned_term_registry.TONE, Visibility.PUBLIC, Surface.CORE) is Verdict.ALLOW

    def test_classes_for_surface_matches_the_registry_gate_routing(self) -> None:
        """The registry's per-gate class routing is DERIVED from this policy, not a second table."""
        assert leak_policy.classes_for_surface(Surface.DIFF) == banned_term_registry.GATE_CLASSES["diff"]
        assert leak_policy.classes_for_surface(Surface.CORE) == banned_term_registry.GATE_CLASSES["core"]
        assert leak_policy.classes_for_surface(Surface.TREE) == banned_term_registry.GATE_CLASSES["tree"]


class TestScansOnVisibility:
    """Everything the gate cannot PROVE non-public is scanned — only NON_PUBLIC skips."""

    def test_public_and_unknown_scan_and_non_public_skips(self) -> None:
        assert scans_on_visibility(Visibility.PUBLIC) is True
        assert scans_on_visibility(Visibility.UNKNOWN) is True
        assert scans_on_visibility(Visibility.NON_PUBLIC) is False


class TestDestinationVisibilityFailsClosed:
    """An unresolvable destination is UNKNOWN (fail-closed → scan), never NON_PUBLIC."""

    def test_an_empty_slug_is_unknown(self) -> None:
        assert destination_visibility(Destination(slug="", via="flag")) is Visibility.UNKNOWN

    def test_an_unexpanded_var_slug_is_unknown(self) -> None:
        # ``$OWNER`` could expand at run time to a PUBLIC repo, so it is scanned.
        assert destination_visibility(Destination(slug="$OWNER/repo", via="flag")) is Visibility.UNKNOWN


class TestLocalCommitWarns:
    """A LOCAL commit downgrades to WARN — the #703 pre-push gate is the real block."""

    @pytest.mark.parametrize("term_class", _BLOCKING_CLASSES)
    @pytest.mark.parametrize("visibility", [Visibility.PUBLIC, Visibility.UNKNOWN])
    def test_local_commit_warns_where_a_publish_blocks(self, term_class: str, visibility: Visibility) -> None:
        assert leak_policy.decide(term_class, visibility, Surface.LOCAL_COMMIT) is Verdict.WARN
        assert leak_policy.decide(term_class, visibility, Surface.PUBLISH) is Verdict.BLOCK


class TestClassOfTerm:
    """A matched term resolves to the class :func:`leak_policy.decide` is asked about."""

    @staticmethod
    def _registry_env(monkeypatch: pytest.MonkeyPatch, registry: dict[str, list[str]]) -> None:
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps(registry))

    def test_a_registered_term_resolves_to_its_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._registry_env(monkeypatch, {"leak": ["acme"], "tone": ["blunt"], "allow": ["widget"]})
        assert banned_term_registry.class_of_term("acme") == banned_term_registry.LEAK
        assert banned_term_registry.class_of_term("Blunt") == banned_term_registry.TONE
        assert banned_term_registry.class_of_term("widget") == banned_term_registry.ALLOW

    def test_the_widest_class_wins_a_term_listed_twice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._registry_env(monkeypatch, {"leak": ["acme"], "tone": ["acme"]})
        assert banned_term_registry.class_of_term("acme") == banned_term_registry.LEAK

    def test_an_unclassifiable_term_lands_in_a_blocking_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Never :data:`ALLOW` — an unknown term must not be able to silence the gate."""
        self._registry_env(monkeypatch, {"leak": ["acme"]})
        resolved = banned_term_registry.class_of_term("never-registered")
        assert leak_policy.decide(resolved, Visibility.PUBLIC, Surface.DIFF) is Verdict.BLOCK

    def test_an_unset_registry_resolves_to_a_blocking_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEATREE_TERM_REGISTRY", raising=False)
        monkeypatch.setattr(banned_term_registry, "load_registry", lambda **_kw: None)
        resolved = banned_term_registry.class_of_term("acme")
        assert leak_policy.decide(resolved, Visibility.PUBLIC, Surface.DIFF) is Verdict.BLOCK
