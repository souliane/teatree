"""F2.3: the six opt-in gates thread the TICKET's overlay into ``get_effective_settings``.

Each ``*_required()`` gained an ``overlay: str | None = None`` parameter and passes
it to :func:`teatree.config.get_effective_settings`, and every ``check_*`` /
``*_satisfied`` wrapper passes ``ticket.overlay or None``. Without this a
per-overlay opt-in bound only when the evaluating process carried a matching
ambient ``T3_OVERLAY_NAME`` — the merge keystone runs env-less, so the gate
silently evaluated OFF (a fail-toward-green hole). These tests pin that the
overlay reaches the resolver.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.core.gates import (
    anti_vacuity_gate,
    integration_review_gate,
    review_context_gate,
    review_request_state_gate,
    rubric_gate,
    spec_coverage_gate,
)

# (module, required-fn name, the UserSettings field it reads)
_REQUIRED_CASES = [
    (rubric_gate, "rubric_gate_required", "require_rubric_verification"),
    (anti_vacuity_gate, "anti_vacuity_required", "require_anti_vacuity_attestation"),
    (review_context_gate, "review_context_required", "require_review_context"),
    (review_request_state_gate, "reviewed_state_required", "require_reviewed_state_for_review_request"),
    (spec_coverage_gate, "spec_coverage_required", "require_spec_coverage"),
    (integration_review_gate, "integration_review_required", "require_integration_review"),
]


@pytest.mark.parametrize(("module", "func", "field"), _REQUIRED_CASES)
def test_required_threads_explicit_overlay(module: object, func: str, field: str) -> None:
    captured: dict[str, object] = {}

    def _fake(overlay: str | None = None) -> SimpleNamespace:
        captured["overlay"] = overlay
        return SimpleNamespace(**{field: True})

    with patch.object(module, "get_effective_settings", _fake):
        assert getattr(module, func)("acme-overlay") is True
    assert captured["overlay"] == "acme-overlay"


@pytest.mark.parametrize(("module", "func", "field"), _REQUIRED_CASES)
def test_required_defaults_to_none_overlay(module: object, func: str, field: str) -> None:
    captured: dict[str, object] = {}

    def _fake(overlay: str | None = None) -> SimpleNamespace:
        captured["overlay"] = overlay
        return SimpleNamespace(**{field: False})

    with patch.object(module, "get_effective_settings", _fake):
        getattr(module, func)()
    assert captured["overlay"] is None


def _off_capture(captured: dict[str, object]):
    """A ``*_required`` stub that records the overlay it was passed and returns OFF.

    Returning ``False`` short-circuits every ``check_*`` before it touches the DB,
    so the test isolates the overlay-threading contract from the gate body.
    """

    def _req(overlay: str | None = None) -> bool:
        captured["o"] = overlay
        return False

    return _req


def test_check_rubric_threads_ticket_overlay() -> None:
    ticket = SimpleNamespace(overlay="acme", pk=1)
    captured: dict[str, object] = {}
    with patch.object(rubric_gate, "rubric_gate_required", _off_capture(captured)):
        rubric_gate.check_rubric_satisfied(ticket, "sha", transition="merge")
    assert captured["o"] == "acme"


def test_check_anti_vacuity_threads_ticket_overlay() -> None:
    ticket = SimpleNamespace(overlay="acme", pk=1)
    captured: dict[str, object] = {}
    with patch.object(anti_vacuity_gate, "anti_vacuity_required", _off_capture(captured)):
        anti_vacuity_gate.check_anti_vacuity_attestation(ticket, "sha", transition="merge")
    assert captured["o"] == "acme"


def test_check_review_context_threads_ticket_overlay() -> None:
    ticket = SimpleNamespace(overlay="acme", pk=1, extra={})
    captured: dict[str, object] = {}
    with patch.object(review_context_gate, "review_context_required", _off_capture(captured)):
        review_context_gate.check_review_context(ticket)
    assert captured["o"] == "acme"


def test_review_context_satisfied_threads_ticket_overlay() -> None:
    ticket = SimpleNamespace(overlay="acme", pk=1, extra={})
    captured: dict[str, object] = {}
    with patch.object(review_context_gate, "review_context_required", _off_capture(captured)):
        assert review_context_gate.review_context_satisfied(ticket) is True
    assert captured["o"] == "acme"


def test_check_reviewed_state_threads_ticket_overlay() -> None:
    ticket = SimpleNamespace(overlay="acme", pk=1)
    captured: dict[str, object] = {}
    with patch.object(review_request_state_gate, "reviewed_state_required", _off_capture(captured)):
        assert review_request_state_gate.check_reviewed_state(ticket) == ""
    assert captured["o"] == "acme"


def test_check_spec_coverage_threads_ticket_overlay() -> None:
    ticket = SimpleNamespace(overlay="acme", pk=1, extra={})
    captured: dict[str, object] = {}
    with patch.object(spec_coverage_gate, "spec_coverage_required", _off_capture(captured)):
        spec_coverage_gate.check_spec_coverage(ticket)
    assert captured["o"] == "acme"


def test_check_integration_review_threads_ticket_overlay() -> None:
    ticket = SimpleNamespace(overlay="acme", pk=1, repos=[], extra={})
    captured: dict[str, object] = {}
    with patch.object(integration_review_gate, "integration_review_required", _off_capture(captured)):
        integration_review_gate.check_integration_review(ticket)
    assert captured["o"] == "acme"


def test_empty_ticket_overlay_becomes_none() -> None:
    # A ticket with a blank overlay resolves the AMBIENT overlay (None), never
    # ``get_effective_settings("")`` (which would resolve nothing).
    ticket = SimpleNamespace(overlay="", pk=1)
    captured: dict[str, object] = {}
    with patch.object(rubric_gate, "rubric_gate_required", _off_capture(captured)):
        rubric_gate.check_rubric_satisfied(ticket, "sha", transition="merge")
    assert captured["o"] is None
