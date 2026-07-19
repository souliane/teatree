"""Tests for the egress-chokepoint wrapper ``scan_outbound_text`` (#1295).

``scan_outbound_text`` derives public-ness from the bash gates' visibility axis
(``_target_is_public`` → ``is_public_destination``) — NOT a per-overlay
``public_repos`` list — and delegates to the pure :func:`scan_for_publication`
with the overlay's redact/quote rules plus the always-on built-in quote anchors.

The MANDATORY anti-inertness test (``test_real_config_blocks_quote_anchor_leak_to_public_repo``)
mocks NONE of the gate's rules or public-ness resolution — only the network
visibility probe — and proves a leaking body to ``souliane/teatree`` is BLOCKED
against the real resolved config (the gap the prior version shipped: empty
``public_repos`` made the gate inert).
"""

from collections.abc import Sequence
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

from teatree.core.gates import privacy_gate
from teatree.core.gates.privacy_gate import scan_outbound_text
from teatree.hooks import _repo_visibility, publish_destination

REAL_PUBLIC = "souliane/teatree"
REDACT = "SECRETCORP"


@pytest.fixture
def inject_rules(monkeypatch: pytest.MonkeyPatch):
    def _set(*, public: bool, redact: Sequence[str] = (), block: Sequence[str] = ()) -> None:
        monkeypatch.setattr(privacy_gate, "_target_is_public", lambda _repo, _forge: public)
        monkeypatch.setattr(privacy_gate, "_overlay_privacy_rules", lambda: (list(redact), list(block)))

    return _set


def test_real_config_blocks_quote_anchor_leak_to_public_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    # No mock of the overlay rules or public-ness — only the network probe — so
    # the block comes from the REAL resolved config: souliane/teatree is public
    # via the visibility axis and the built-in quote anchors always fire.
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    result = scan_outbound_text(
        text="Posting what the user said verbatim to the public PR.",
        target_repo=REAL_PUBLIC,
        forge="github",
    )
    assert result.refused
    assert result.is_public


def test_real_config_public_repo_scanned_even_when_probe_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Probe returns None (tool absent) → the visibility axis fails CLOSED to
    # public, so the leak is still blocked — detection failure never disables it.
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
    monkeypatch.setattr(_repo_visibility, "_read_visibility_cache", lambda _slug: None)
    result = scan_outbound_text(
        text="The user said this verbatim and it must not leak.",
        target_repo=REAL_PUBLIC,
        forge="github",
    )
    assert result.refused


def test_blocks_overlay_redact_term_on_public_repo(inject_rules) -> None:
    inject_rules(public=True, redact=[REDACT])
    result = scan_outbound_text(text=f"This review note touches {REDACT} internals.", target_repo=REAL_PUBLIC)
    assert result.refused
    assert any(match.pattern_name == f"redact:{REDACT}" for match in result.matches)


def test_blocks_builtin_quote_anchor_with_no_overlay_terms(inject_rules) -> None:
    inject_rules(public=True)
    result = scan_outbound_text(text="The user said this verbatim in chat.", target_repo=REAL_PUBLIC)
    assert result.refused


def test_clean_body_passes(inject_rules) -> None:
    inject_rules(public=True, redact=[REDACT])
    result = scan_outbound_text(text="A perfectly ordinary review note.", target_repo=REAL_PUBLIC)
    assert not result.refused


def test_private_target_passes_same_leaking_content(inject_rules) -> None:
    inject_rules(public=False, redact=[REDACT])
    result = scan_outbound_text(text=f"This touches {REDACT} verbatim user said.", target_repo="acme/private")
    assert not result.refused
    assert result.is_public is False


def test_classification_error_fails_closed_to_scanning(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError

    # ``_target_is_public`` re-imports ``is_public_destination`` per call, so
    # patching it here makes the classification raise → the gate must fail
    # CLOSED (treat as public, scan), so the built-in anchor still blocks.
    monkeypatch.setattr(publish_destination, "is_public_destination", _boom)
    monkeypatch.setattr(privacy_gate, "_overlay_privacy_rules", lambda: ([], []))
    result = scan_outbound_text(text="User mandate (verbatim leak here.", target_repo=REAL_PUBLIC, forge="github")
    assert result.refused


def test_overlay_redact_rules_empty_when_overlay_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise ImproperlyConfigured

    monkeypatch.setattr(privacy_gate, "get_overlay", _raise)
    assert privacy_gate._overlay_privacy_rules() == ([], [])


def test_overlay_redact_rules_read_from_resolved_overlay_config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(privacy_redact_terms=[REDACT], privacy_block_patterns=["custom-pattern"])
    monkeypatch.setattr(privacy_gate, "get_overlay", lambda *_a, **_k: SimpleNamespace(config=config))
    assert privacy_gate._overlay_privacy_rules() == ([REDACT], ["custom-pattern"])


# --- F2.2: overlay-rule RESOLUTION FAILURE fails the public publish CLOSED + loud ---

_RESOLUTION_FAILURE_MSG = "overlay registry wedged"


def test_overlay_rules_none_on_genuine_resolution_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # An overlay IS present but reading it raises an UNEXPECTED error (not the
    # ImproperlyConfigured that means "no single overlay"). The overlay's own
    # redact/block rules would silently vanish, so the resolver returns None.
    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError(_RESOLUTION_FAILURE_MSG)

    monkeypatch.setattr(privacy_gate, "get_overlay", _boom)
    assert privacy_gate._overlay_privacy_rules() is None


def test_overlay_rules_none_when_config_fields_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    # get_overlay() resolves, but reading the privacy fields off the config raises
    # → a present-but-unreadable overlay is a resolution failure → None (fail closed).
    class _AngryConfig:
        @property
        def privacy_redact_terms(self) -> list[str]:
            raise RuntimeError(_RESOLUTION_FAILURE_MSG)

        @property
        def privacy_block_patterns(self) -> list[str]:
            return []

    monkeypatch.setattr(privacy_gate, "get_overlay", lambda *_a, **_k: SimpleNamespace(config=_AngryConfig()))
    assert privacy_gate._overlay_privacy_rules() is None


def test_public_publish_refused_when_overlay_rules_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    # The confidentiality boundary: a PUBLIC target whose overlay rules cannot be
    # resolved is REFUSED (fail closed + loud), NOT scanned with only the built-ins.
    monkeypatch.setattr(privacy_gate, "_target_is_public", lambda _repo, _forge: True)
    monkeypatch.setattr(privacy_gate, "_overlay_privacy_rules", lambda: None)
    result = scan_outbound_text(text="A perfectly ordinary note.", target_repo=REAL_PUBLIC, forge="github")
    assert result.refused
    assert result.is_public
    assert any(match.pattern_name == "overlay-rules-unresolvable" for match in result.matches)


def test_private_target_not_refused_even_when_rules_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A provably-PRIVATE target is a clean pass regardless of rule resolution —
    # the fail-closed refusal is scoped to public targets only.
    monkeypatch.setattr(privacy_gate, "_target_is_public", lambda _repo, _forge: False)
    monkeypatch.setattr(privacy_gate, "_overlay_privacy_rules", lambda: None)
    result = scan_outbound_text(text="A note.", target_repo="acme/private", forge="github")
    assert not result.refused
    assert result.is_public is False
