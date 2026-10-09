"""Situational honesty-critical escalation routing to the most-honest model (#2263).

``resolve_spawn_model`` raises a VERIFICATION-phase spawn to ``[agent]
honesty_model`` (default ``"opus"`` — #2237 removal: no separate kill-switch,
just an explicit-opt-in-free default) when an active
:class:`~teatree.core.models.honesty_escalation.HonestyEscalation` row exists for
the session. The must-fire / must-not-fire twin defeats vacuity:

- must-fire — active row + ``testing`` (baseline balanced/sonnet) → raised to
    the frontier tier model (opus).
- negative control — NO row + ``reviewing`` → the exact baseline (the frontier
    tier model — regressing ``DEFAULT_PHASE_MODELS["reviewing"]`` fails THIS
    test).
- situational scope — a non-verification phase (``shipping``, baseline
    balanced) never escalates even with an active row (stays balanced).
- auto-clear — an expired row resolves back to the baseline tier model (no cron;
    the clock is the only mock).
- #4 backstop — a rubric-gate refusal writes the ``shipped_incomplete`` row.
- model unit — ``is_active`` honors ``cleared_at`` + ``expires_at``, and
    ``mark_cleared`` is the explicit clear on an honest landing.
"""

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from teatree.agents.model_tiering import TIER_MODELS, resolve_spawn_model
from teatree.core.models.honesty_escalation import _DEFAULT_TTL, HonestyEscalation

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SESSION = "11111111-2222-3333-4444-555555555555"


def _seed(db_path: Path, key: str, value: object, scope: str = "") -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)"
    )
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
        (scope, key, json.dumps(value)),
    )
    conn.commit()
    conn.close()


def _seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **keys: object) -> None:
    """Seed a temp config DB with each ``key=value`` and point ``T3_CONFIG_DB`` at it."""
    db = tmp_path / "db.sqlite3"
    for key, value in keys.items():
        _seed(db, key, value)
    monkeypatch.setenv("T3_CONFIG_DB", str(db))


def _raise_db_down(*_args: object, **_kwargs: object) -> bool:
    """A stand-in for ``HonestyEscalation.is_active`` that fails (DB error)."""
    raise RuntimeError


class TestEscalationRouting:
    """The verification-phase escalation branch in ``resolve_spawn_model``."""

    def test_escalation_routes_verification_to_honesty_model(self) -> None:
        # An active escalation raises a verification phase to the honesty model.
        # "testing" starts at the balanced tier (sonnet), so a raise to the
        # default honesty_model ("opus", a literal pass-through — not a
        # TIER_MODELS key) is observable.
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        resolved = resolve_spawn_model("testing", skills=[], session_id=SESSION)
        assert resolved == "opus"

    def test_no_escalation_stays_baseline(self) -> None:
        # NEGATIVE CONTROL: the same call with NO row resolves to the exact
        # DEFAULT_PHASE_MODELS["reviewing"] baseline (the frontier tier model).
        resolved = resolve_spawn_model("reviewing", skills=[], session_id=SESSION)
        assert resolved == TIER_MODELS["frontier"]

    def test_no_session_id_never_escalates(self) -> None:
        # A spawn with no session id (both call-site ids absent) can never match
        # a row → byte-identical to today's baseline. "testing" (balanced) makes
        # an incorrect escalation observable.
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        assert resolve_spawn_model("testing", skills=[], session_id=None) == TIER_MODELS["balanced"]

    def test_non_verification_phase_never_escalates(self) -> None:
        # An active row does NOT escalate a non-verification phase. "shipping"
        # (balanced) makes an incorrect escalation to opus observable.
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        resolved = resolve_spawn_model("shipping", skills=[], session_id=SESSION)
        assert resolved == TIER_MODELS["balanced"]

    def test_every_verification_phase_escalates(self) -> None:
        # "reviewing" is already frontier tier — tied with honesty_model="opus",
        # so it stays the frontier model literal; the below-frontier phases raise
        # to the honesty_model literal ("opus", a pass-through, not a tier key).
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        expected = {
            "reviewing": TIER_MODELS["frontier"],
            "requesting_review": "opus",
            "testing": "opus",
        }
        for phase, want in expected.items():
            resolved = resolve_spawn_model(phase, skills=[], session_id=SESSION)
            assert resolved == want, phase

    def test_expired_escalation_auto_clears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Record a row, then advance the clock past the TTL: is_active returns
        # False (read-time auto-clear, no cron) so routing falls back to balanced.
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        future = timezone.now() + _DEFAULT_TTL + timedelta(minutes=1)
        monkeypatch.setattr(timezone, "now", lambda: future)
        assert resolve_spawn_model("testing", skills=[], session_id=SESSION) == TIER_MODELS["balanced"]

    def test_cleared_escalation_no_longer_routes(self) -> None:
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        HonestyEscalation.mark_cleared(SESSION)
        assert resolve_spawn_model("testing", skills=[], session_id=SESSION) == TIER_MODELS["balanced"]

    def test_honesty_model_config_overrides_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # honesty_model is config-driven: pointing it at the frontier tier routes
        # the escalated verification spawn to that tier's model (the "most-honest
        # model" is a one-line edit).
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        _seeded_db(tmp_path, monkeypatch, agent_honesty_model="frontier")
        assert resolve_spawn_model("reviewing", skills=[], session_id=SESSION) == TIER_MODELS["frontier"]

    def test_escalation_only_raises_never_lowers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A phase already pinned ABOVE the honesty model is not lowered: most-
        # capable-wins. honesty_model=cheap must not downgrade a frontier phase.
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        _seeded_db(
            tmp_path,
            monkeypatch,
            agent_honesty_model="cheap",
            agent_phase_models={"reviewing": "frontier"},
        )
        assert resolve_spawn_model("reviewing", skills=[], session_id=SESSION) == TIER_MODELS["frontier"]


class TestHonestyEscalationActiveFailSafe:
    """``_honesty_escalation_active`` is fail-SAFE to no-escalation."""

    def test_resolution_error_is_no_escalation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A resolution error must NEVER silently escalate: it returns the
        # baseline. Force is_active to raise; the verification phase stays
        # balanced (sonnet) — "testing" makes an incorrect escalation observable.
        import teatree.agents.model_tiering as mt_mod  # noqa: PLC0415

        monkeypatch.setattr(HonestyEscalation, "is_active", _raise_db_down)
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        assert mt_mod._honesty_escalation_active(SESSION, None) is False
        assert resolve_spawn_model("testing", skills=[], session_id=SESSION) == TIER_MODELS["balanced"]


class TestHonestyEscalationModel:
    """``HonestyEscalation.record`` / ``is_active`` / ``mark_cleared`` unit contract."""

    def test_record_creates_active_row(self) -> None:
        row = HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        assert row is not None
        assert row.reason == HonestyEscalation.Reason.USER_ASKED
        assert row.cleared_at is None
        assert row.expires_at > timezone.now()
        assert HonestyEscalation.is_active(SESSION) is True

    def test_record_is_idempotent_on_session_task_reason(self) -> None:
        first = HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        second = HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        assert first is not None
        assert second is None
        assert HonestyEscalation.objects.filter(session_id=SESSION).count() == 1

    def test_blank_session_id_refused(self) -> None:
        assert HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id="") is None
        assert HonestyEscalation.is_active("") is False

    def test_is_active_honors_cleared_at(self) -> None:
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        assert HonestyEscalation.is_active(SESSION) is True
        cleared = HonestyEscalation.mark_cleared(SESSION)
        assert cleared == 1
        assert HonestyEscalation.is_active(SESSION) is False

    def test_is_active_honors_expires_at(self) -> None:
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION, ttl=timedelta(seconds=-1))
        # An already-expired row is never active (read-time auto-clear).
        assert HonestyEscalation.is_active(SESSION) is False

    def test_mark_cleared_is_idempotent(self) -> None:
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        assert HonestyEscalation.mark_cleared(SESSION) == 1
        assert HonestyEscalation.mark_cleared(SESSION) == 0

    def test_distinct_reasons_are_distinct_rows(self) -> None:
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        HonestyEscalation.record(HonestyEscalation.Reason.SHIPPED_INCOMPLETE, session_id=SESSION)
        assert HonestyEscalation.objects.filter(session_id=SESSION).count() == 2

    def test_session_wide_row_is_active_for_any_task(self) -> None:
        # A ticket-wide row (task_id=None) fires for every task in the session.
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        assert HonestyEscalation.is_active(SESSION, task_id=7) is True
        assert HonestyEscalation.is_active(SESSION) is True

    def test_task_scoped_row_does_not_fire_for_sibling_task(self) -> None:
        # A task-scoped row fires only for its own task, not a sibling, and not
        # an unscoped (session-wide) query.
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION, task_id=7)
        assert HonestyEscalation.is_active(SESSION, task_id=7) is True
        assert HonestyEscalation.is_active(SESSION, task_id=8) is False
        assert HonestyEscalation.is_active(SESSION) is False
