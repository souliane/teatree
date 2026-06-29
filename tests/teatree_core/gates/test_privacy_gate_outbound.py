"""Tests for the egress-chokepoint wrapper ``scan_outbound_text`` (#1295).

``scan_outbound_text`` resolves the active overlay's ``public_repos`` plus
redact/quote rules and delegates to the pure :func:`scan_for_publication`.
The overlay resolution (``_overlay_publication_rules``) is injected here so
the wrapper's gating logic is exercised without external overlay config.
"""

from collections.abc import Sequence
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

from teatree.core import overlay_loader
from teatree.core.gates import privacy_gate
from teatree.core.gates.privacy_gate import scan_outbound_text

PUBLIC = "owner/pub-repo"
REDACT = "SECRETCORP"


@pytest.fixture
def inject_rules(monkeypatch: pytest.MonkeyPatch):
    def _set(public_repos: Sequence[str], redact: Sequence[str] = (), block: Sequence[str] = ()) -> None:
        monkeypatch.setattr(
            privacy_gate,
            "_overlay_publication_rules",
            lambda: (list(public_repos), list(redact), list(block)),
        )

    return _set


def test_blocks_overlay_redact_term_on_public_repo(inject_rules) -> None:
    inject_rules([PUBLIC], redact=[REDACT])
    result = scan_outbound_text(text=f"This review note touches {REDACT} internals.", target_repo=PUBLIC)
    assert result.refused
    assert any(match.pattern_name == f"redact:{REDACT}" for match in result.matches)


def test_blocks_builtin_quote_anchor_with_no_overlay_terms(inject_rules) -> None:
    inject_rules([PUBLIC])
    result = scan_outbound_text(text="The user said this verbatim in chat.", target_repo=PUBLIC)
    assert result.refused


def test_clean_body_passes(inject_rules) -> None:
    inject_rules([PUBLIC], redact=[REDACT])
    result = scan_outbound_text(text="A perfectly ordinary review note.", target_repo=PUBLIC)
    assert not result.refused


def test_private_target_passes_same_leaking_content(inject_rules) -> None:
    inject_rules([PUBLIC], redact=[REDACT])
    result = scan_outbound_text(text=f"This touches {REDACT} internals.", target_repo="other/private-repo")
    assert not result.refused
    assert result.is_public is False


def test_no_resolvable_overlay_is_a_clean_noop(inject_rules) -> None:
    inject_rules([])
    result = scan_outbound_text(text=f"{REDACT} — user said this verbatim.", target_repo=PUBLIC)
    assert not result.refused


def test_overlay_rules_read_from_resolved_overlay_config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(
        public_repos=[PUBLIC],
        privacy_redact_terms=[REDACT],
        privacy_block_patterns=["custom-pattern"],
    )
    monkeypatch.setattr(overlay_loader, "get_overlay", lambda *_a, **_k: SimpleNamespace(config=config))
    assert privacy_gate._overlay_publication_rules() == ([PUBLIC], [REDACT], ["custom-pattern"])


def test_overlay_rules_empty_when_overlay_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise ImproperlyConfigured

    monkeypatch.setattr(overlay_loader, "get_overlay", _raise)
    assert privacy_gate._overlay_publication_rules() == ([], [], [])
