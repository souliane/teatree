"""A matched registry term is named by ``class#index``; the value never leaves the registry."""

import json

import pytest

from teatree.hooks.banned_term_registry import load_term_refs
from teatree.hooks.term_refs import UNLISTED_REF, TermRefs

_REGISTRY = {
    "leak": ("alpha-term", "beta-term"),
    "prose_collider": ("gamma-term", "beta-term"),
    "tone": (),
    "overlay": ("delta-term",),
    "allow": ("alpha-term", "epsilon-term"),
}


def test_a_term_is_named_by_its_class_and_position() -> None:
    refs = TermRefs(_REGISTRY)

    assert refs.ref("beta-term") == "leak#1"
    assert refs.ref("gamma-term") == "prose_collider#0"
    assert refs.ref("delta-term") == "overlay#0"


def test_the_earliest_class_wins_a_term_listed_twice_and_allow_never_names_a_finding() -> None:
    refs = TermRefs(_REGISTRY)

    assert refs.ref("alpha-term") == "leak#0"
    assert refs.ref("epsilon-term") == UNLISTED_REF


def test_a_values_scan_names_exactly_its_classes_allow_included() -> None:
    refs = TermRefs.of_classes(_REGISTRY, ("leak", "overlay", "allow"))

    assert refs.ref("alpha-term") == "leak#0"
    assert refs.ref("epsilon-term") == "allow#1"
    assert refs.ref("gamma-term") == UNLISTED_REF


def test_lookup_ignores_case_and_surrounding_space() -> None:
    assert TermRefs(_REGISTRY).ref("  GAMMA-Term ") == "prose_collider#0"


def test_a_term_the_registry_lacks_is_named_by_its_scan_position() -> None:
    refs = TermRefs(None, ("first-term", "second-term"))

    assert refs.ref("second-term") == "scan#1"
    assert refs.ref("unknown-term") == UNLISTED_REF


@pytest.mark.parametrize("raw", ["{not json", json.dumps(["not", "a", "table"])])
def test_an_unreadable_registry_falls_back_to_scan_positions(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEATREE_TERM_REGISTRY", raw)

    assert load_term_refs(("only-term",)).ref("only-term") == "scan#0"


def test_load_term_refs_reads_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": ["x-term", "y-term"]}))

    assert load_term_refs(("y-term",)).ref("y-term") == "leak#1"
