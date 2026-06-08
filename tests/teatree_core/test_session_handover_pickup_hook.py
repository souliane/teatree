"""SessionStart picks up an unclaimed session hand-off and injects it.

The zero-copy-paste takeover: a fresh / non-owner session claims a hand-off
(targeted AT it, or parked for "next session") on SessionStart and injects
the handing session's durable-state snapshot as ``additionalContext``,
marked claimed so it injects exactly once.
"""

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest import mock

from django.test import TestCase

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _claim_session_handover, handle_session_start_bootstrap
from teatree.core.models import SessionHandover


def _bootstrap_stdout(data: dict) -> str:
    """Run the SessionStart handler, returning its single stdout JSON write."""
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        handle_session_start_bootstrap(data)
    return buf.getvalue()


class _RegistryIsolation(TestCase):
    """Point the loop registry + tty sink at temp paths (no real machine state).

    The teatree opt-in marker AND the #256 session-start auto-load opt-in are
    forced active so the SessionStart bootstrap fires: these tests cover the
    handover-pickup mechanism, not the opt-in gates (covered by
    ``test_teatree_opt_in.py``).
    """

    def setUp(self) -> None:
        super().setUp()
        reg_dir = Path(self.enterContext(_tmp_dir()))
        self.enterContext(_env("T3_LOOP_REGISTRY_DIR", str(reg_dir)))
        self.enterContext(mock.patch.object(router, "_TTY_PATH", str(reg_dir / "fake-tty")))
        self.enterContext(mock.patch.object(router, "_teatree_active", return_value=True))
        self.enterContext(mock.patch.object(router, "_loops_auto_load_enabled", return_value=True))


class TestClaimSessionHandover(_RegistryIsolation):
    def test_returns_none_when_nothing_to_claim(self) -> None:
        assert _claim_session_handover("fresh") is None

    def test_claims_handover_targeted_at_session(self) -> None:
        SessionHandover.objects.create_handover(from_session="prev", to_session="me", payload="WORK STATE")
        directive = _claim_session_handover("me")
        assert directive is not None
        assert "WORK STATE" in directive
        assert "from session `prev`" in directive
        assert SessionHandover.objects.get().claimed_by == "me"

    def test_claims_parked_for_next_session(self) -> None:
        SessionHandover.objects.create_handover(from_session="prev", to_session="", payload="PARKED")
        directive = _claim_session_handover("whoever")
        assert directive is not None
        assert "PARKED" in directive

    def test_injects_once_only(self) -> None:
        SessionHandover.objects.create_handover(from_session="prev", to_session="me", payload="WORK")
        assert _claim_session_handover("me") is not None
        assert _claim_session_handover("me") is None


class TestSessionStartInjectsHandover(_RegistryIsolation):
    def test_fresh_session_directive_includes_handover_payload(self) -> None:
        SessionHandover.objects.create_handover(from_session="prev", to_session="newcomer", payload="RESUME ME")
        ctx = json.loads(_bootstrap_stdout({"session_id": "newcomer"}))["hookSpecificOutput"]["additionalContext"]
        assert "SESSION HAND-OFF RECEIVED" in ctx
        assert "RESUME ME" in ctx

    def test_session_does_not_claim_its_own_handover_on_compact_resume(self) -> None:
        SessionHandover.objects.create_handover(from_session="self", to_session="", payload="MINE")
        ctx = json.loads(_bootstrap_stdout({"session_id": "self", "source": "compact"}))["hookSpecificOutput"][
            "additionalContext"
        ]
        assert "SESSION HAND-OFF RECEIVED" not in ctx
        assert SessionHandover.objects.get().claimed_at is None


@contextlib.contextmanager
def _tmp_dir() -> Iterator[str]:
    with tempfile.TemporaryDirectory() as directory:
        yield directory


@contextlib.contextmanager
def _env(var: str, value: str) -> Iterator[None]:
    prior = os.environ.get(var)
    os.environ[var] = value
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = prior
