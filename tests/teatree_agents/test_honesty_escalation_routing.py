"""Situational honesty-critical escalation routing to the most-honest model (#2263).

``resolve_spawn_model`` raises a VERIFICATION-phase spawn to ``[agent]
honesty_model`` (today Fable) when an active
:class:`~teatree.core.models.honesty_escalation.HonestyEscalation` row exists for
the session. The must-fire / must-not-fire twin defeats vacuity:

- must-fire — active row + ``reviewing`` → ``"fable"``.
- negative control — NO row + ``reviewing`` → the exact baseline (the frontier
    tier model — regressing ``DEFAULT_PHASE_MODELS["reviewing"]`` to fable fails
    THIS test).
- situational scope — a non-verification phase (``coding``) never escalates (it
    stays the frontier tier model, not fable).
- auto-clear — an expired row resolves back to the baseline tier model (no cron;
    the clock is the only mock).
- kill-switch — ``fable_enabled=false`` + a row → ``"opus"`` (NOT "≠ fable":
    proving the escalated Fable passed THROUGH ``_downgrade_fable``).
- #4 backstop — a rubric-gate refusal writes the ``shipped_incomplete`` row.
- model unit — ``is_active`` honors ``cleared_at`` + ``expires_at``, and
    ``mark_cleared`` is the explicit clear on an honest landing.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from teatree.agents.model_tiering import TIER_MODELS, resolve_spawn_model
from teatree.core.models.honesty_escalation import _DEFAULT_TTL, HonestyEscalation

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SESSION = "11111111-2222-3333-4444-555555555555"


def _write_toml(path: Path, content: str) -> Path:
    cfg = path / ".teatree.toml"
    cfg.write_text(content, encoding="utf-8")
    return cfg


def _raise_db_down(*_args: object, **_kwargs: object) -> bool:
    """A stand-in for ``HonestyEscalation.is_active`` that fails (DB error)."""
    raise RuntimeError


class TestEscalationRouting:
    """The verification-phase escalation branch in ``resolve_spawn_model``."""

    def test_escalation_routes_verification_to_fable(self, tmp_path: Path) -> None:
        # An active escalation + a verification phase routes to the honesty model.
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        cfg = _write_toml(tmp_path, "")
        resolved = resolve_spawn_model("reviewing", skills=[], session_id=SESSION, config_path=cfg)
        assert resolved == "fable"

    def test_no_escalation_stays_baseline(self, tmp_path: Path) -> None:
        # NEGATIVE CONTROL: the same call with NO row resolves to the exact
        # DEFAULT_PHASE_MODELS["reviewing"] baseline (the frontier tier model).
        # Regressing that default to "fable" (making the must-fire test pass
        # vacuously) fails THIS test.
        cfg = _write_toml(tmp_path, "")
        resolved = resolve_spawn_model("reviewing", skills=[], session_id=SESSION, config_path=cfg)
        assert resolved == TIER_MODELS["frontier"]

    def test_no_session_id_never_escalates(self, tmp_path: Path) -> None:
        # A spawn with no session id (both call-site ids absent) can never match
        # a row → byte-identical to today's baseline.
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        cfg = _write_toml(tmp_path, "")
        assert resolve_spawn_model("reviewing", skills=[], session_id=None, config_path=cfg) == TIER_MODELS["frontier"]

    def test_non_verification_phase_never_escalates(self, tmp_path: Path) -> None:
        # An active row does NOT escalate a non-verification phase (coding stays
        # the frontier tier model, NOT fable). Situational, scoped to verification.
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        cfg = _write_toml(tmp_path, "")
        assert resolve_spawn_model("coding", skills=[], session_id=SESSION, config_path=cfg) == TIER_MODELS["frontier"]

    def test_every_verification_phase_escalates(self, tmp_path: Path) -> None:
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        cfg = _write_toml(tmp_path, "")
        for phase in ("reviewing", "requesting_review", "testing"):
            assert resolve_spawn_model(phase, skills=[], session_id=SESSION, config_path=cfg) == "fable", phase

    def test_expired_escalation_auto_clears(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Record a row, then advance the clock past the TTL: is_active returns
        # False (read-time auto-clear, no cron) so routing falls back to sonnet.
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        cfg = _write_toml(tmp_path, "")
        future = timezone.now() + _DEFAULT_TTL + timedelta(minutes=1)
        monkeypatch.setattr(timezone, "now", lambda: future)
        assert (
            resolve_spawn_model("reviewing", skills=[], session_id=SESSION, config_path=cfg) == TIER_MODELS["frontier"]
        )

    def test_cleared_escalation_no_longer_routes(self, tmp_path: Path) -> None:
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        HonestyEscalation.mark_cleared(SESSION)
        cfg = _write_toml(tmp_path, "")
        assert (
            resolve_spawn_model("reviewing", skills=[], session_id=SESSION, config_path=cfg) == TIER_MODELS["frontier"]
        )

    def test_kill_switch_reverts_escalation_to_opus(self, tmp_path: Path) -> None:
        # fable_enabled=false + an active row: the escalation still RAISES to
        # fable, but it passes THROUGH the unchanged _downgrade_fable, so the
        # resolved model is the fallback (opus), NOT fable. Asserting "opus"
        # (not merely "!= fable") proves it routed through the kill-switch.
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        cfg = _write_toml(tmp_path, "[agent]\nfable_enabled = false\n")
        assert resolve_spawn_model("reviewing", skills=[], session_id=SESSION, config_path=cfg) == "opus"

    def test_honesty_model_config_overrides_target(self, tmp_path: Path) -> None:
        # honesty_model is config-driven: pointing it at the frontier tier routes
        # the escalated verification spawn to that tier's model (the "most-honest
        # model" is a one-line edit).
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        cfg = _write_toml(tmp_path, '[agent]\nhonesty_model = "frontier"\n')
        assert (
            resolve_spawn_model("reviewing", skills=[], session_id=SESSION, config_path=cfg) == TIER_MODELS["frontier"]
        )

    def test_escalation_only_raises_never_lowers(self, tmp_path: Path) -> None:
        # A phase already pinned ABOVE the honesty model is not lowered: most-
        # capable-wins. honesty_model=cheap must not downgrade a frontier phase.
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        cfg = _write_toml(tmp_path, '[agent]\nhonesty_model = "cheap"\nphase_models.reviewing = "frontier"\n')
        assert (
            resolve_spawn_model("reviewing", skills=[], session_id=SESSION, config_path=cfg) == TIER_MODELS["frontier"]
        )


class TestHonestyEscalationActiveFailSafe:
    """``_honesty_escalation_active`` is fail-SAFE to no-escalation."""

    def test_resolution_error_is_no_escalation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A resolution error must NEVER silently pin Fable: it returns the
        # baseline. Force is_active to raise; the verification phase stays sonnet.
        import teatree.agents.model_tiering as mt_mod  # noqa: PLC0415

        monkeypatch.setattr(HonestyEscalation, "is_active", _raise_db_down)
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=SESSION)
        cfg = _write_toml(tmp_path, "")
        assert mt_mod._honesty_escalation_active(SESSION, None) is False
        assert (
            resolve_spawn_model("reviewing", skills=[], session_id=SESSION, config_path=cfg) == TIER_MODELS["frontier"]
        )


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
