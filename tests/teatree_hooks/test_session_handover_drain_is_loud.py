# test-path: cross-cutting — drives hooks/scripts/session_handover_pickup.py + hook_router.py; no src/teatree/ mirror.
"""A parked hand-off is drained by a starting session, and a failed drain LOGS (#3810).

Six hand-offs sat unclaimed for a week on a live box. Two defects kept them
there and both were invisible:

The drain was stapled to the loop auto-load gate, so a session that did not arm
the loop machinery returned before the hand-off merge ever ran. And every
degradation was swallowed — an unimportable Django and a raising
``claim_handovers`` both left ``payload = ""`` with no trace at all, so a queue
that never drained looked exactly like a queue that was always empty.
"""

import logging
from pathlib import Path
from unittest import mock

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.session_handover_pickup as pickup
from hooks.scripts.hook_router import handle_session_start_bootstrap
from teatree.core.models import SessionHandover


@pytest.fixture(autouse=True)
def _isolation(db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the loop registry and tty sink at temp paths — never touch real state."""
    reg_dir = tmp_path / "data"
    reg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))
    monkeypatch.setattr(router, "_autoload_enabled", lambda: True)
    monkeypatch.setattr(router, "_teatree_active", lambda session_id: True)


class TestHandoverDrainReach:
    def test_a_session_that_does_not_arm_loops_still_drains_the_queue(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Arming the loop machinery and receiving a hand-off are unrelated concerns.

        Gating the drain on the loop gate meant such a session silently stranded
        every parked hand-off for the next one to also strand.
        """
        SessionHandover.objects.create_handover(
            from_session="the-departing-session", to_session="", payload="carry this on"
        )
        monkeypatch.setattr(router, "_loop_auto_load_active", lambda session_id: False)

        handle_session_start_bootstrap({"session_id": "the-fresh-session", "source": "startup"})

        assert "carry this on" in capsys.readouterr().out
        assert SessionHandover.objects.filter(claimed_at__isnull=True).count() == 0


class TestHandoverDrainFailureIsLoud:
    def test_an_unavailable_orm_logs_that_the_queue_was_not_read(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The hook interpreter cannot always import teatree — say so, don't just carry on."""
        monkeypatch.setattr(pickup, "bootstrap_teatree_django", lambda: False)

        with caplog.at_level(logging.WARNING, logger="teatree.hook_router"):
            pickup.claim_session_handover("the-fresh-session")

        assert "SKIPPED" in caplog.text
        assert "the-fresh-session" in caplog.text

    def test_a_raising_drain_logs_the_cause(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failing open is required; failing open in silence is what hid this for a week."""
        monkeypatch.setattr(pickup, "bootstrap_teatree_django", lambda: True)

        with (
            mock.patch("teatree.core.handover.claim_handovers", side_effect=RuntimeError("db is locked")),
            caplog.at_level(logging.WARNING, logger="teatree.hook_router"),
        ):
            result = pickup.claim_session_handover("the-fresh-session")

        assert result is None, "the hook still fails open — the session is never blocked"
        assert "FAILED" in caplog.text
        assert "db is locked" in caplog.text
