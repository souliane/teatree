"""teatree.core.mode_resolution — the unified operating-mode resolver (#61).

Proves the merged ``resolve_active_mode`` satisfies BOTH old surfaces (the
availability ``.defers_questions`` / ``.pauses_self_pump`` predicates and the
preset ``.state_for`` per-loop opinion), the override→schedule→default
precedence, the presence-sensitivity upgrade, and the return-to-reachable drain.
Integration-first against the real DB.
"""

import datetime as dt
import tempfile
from pathlib import Path
from unittest import mock

import django.test
from django.utils import timezone

from teatree.core import availability
from teatree.core.mode_resolution import clear_mode_override, resolve_active_mode, set_mode_override
from teatree.core.models import ConfigSetting, Mode, ModeOverride

_DRAIN = "teatree.core.notify_question_drains.drain_deferred_questions"


class _TmpStateMixin(django.test.TestCase):
    """Repoint the presence heartbeat + availability override file to a per-test tmp dir.

    The resolver reads the live-presence heartbeat and mirrors the resolved posture
    to the availability override file; both default to the shared ``DATA_DIR``.
    Redirecting them to a tmp dir keeps a resolver test from polluting the real
    state files (a fixed-``now`` availability test would otherwise read a stray
    future-dated keystroke as fresh).
    """

    def setUp(self) -> None:
        super().setUp()
        tmp = Path(tempfile.mkdtemp())
        for patcher in (
            mock.patch.object(availability, "PRESENCE", availability.PresenceHeartbeat(lambda: tmp / "presence")),
            mock.patch.object(availability, "override_path", return_value=tmp / "availability_override.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        Mode.objects.all().delete()
        ModeOverride.objects.all().delete()


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestResolveActiveMode(_TmpStateMixin):
    def setUp(self) -> None:
        super().setUp()
        self.engaged = Mode.objects.create(
            name="engaged", entries={"review": True}, defers_questions=False, pauses_self_pump=False
        )
        self.unattended = Mode.objects.create(
            name="unattended", entries={"review": False}, defers_questions=True, pauses_self_pump=False
        )
        self.offline = Mode.objects.create(
            name="offline",
            entries={"review": False},
            defers_questions=True,
            pauses_self_pump=True,
            presence_sensitive=False,
        )

    def test_default_when_no_override_is_the_configured_default_mode(self) -> None:
        resolved = resolve_active_mode()
        assert resolved.source == "default"
        assert resolved.name == "engaged"
        assert resolved.defers_questions is False
        assert resolved.pauses_self_pump is False

    def test_manual_override_wins_and_carries_both_surfaces(self) -> None:
        set_mode_override("offline")
        resolved = resolve_active_mode()
        assert resolved.source == "override"
        assert resolved.name == "offline"
        # availability surface
        assert resolved.defers_questions is True
        assert resolved.pauses_self_pump is True
        # preset surface
        assert resolved.state_for("review") is False

    def test_autonomous_away_defers_but_does_not_pause(self) -> None:
        set_mode_override("unattended")
        resolved = resolve_active_mode()
        assert resolved.defers_questions is True
        assert resolved.pauses_self_pump is False

    def test_missing_default_mode_fails_open_to_present(self) -> None:
        Mode.objects.all().delete()
        resolved = resolve_active_mode()
        assert resolved.defers_questions is False
        assert resolved.pauses_self_pump is False
        assert resolved.state_for("anything") is None  # no opinion → inherit base


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestPresenceUpgrade(_TmpStateMixin):
    def setUp(self) -> None:
        super().setUp()
        Mode.objects.create(name="engaged", entries={}, defers_questions=False)
        # A default away-class mode that IS presence-sensitive.
        self.away_sensitive = Mode.objects.create(
            name="unattended", entries={}, defers_questions=True, presence_sensitive=True
        )
        ConfigSetting.objects.set_value("default_mode", "unattended")

    def _stamp_keystroke(self, *, ago: dt.timedelta) -> None:
        availability.PRESENCE.record(session_id="s", now=timezone.now() - ago)

    def test_fresh_keystroke_upgrades_a_default_away_mode(self) -> None:
        self._stamp_keystroke(ago=dt.timedelta(minutes=1))
        resolved = resolve_active_mode()
        assert resolved.source == "live"
        assert resolved.name == "engaged"
        assert resolved.defers_questions is False

    def test_stale_keystroke_does_not_upgrade(self) -> None:
        self._stamp_keystroke(ago=dt.timedelta(hours=2))
        resolved = resolve_active_mode()
        assert resolved.source == "default"
        assert resolved.defers_questions is True

    def test_manual_override_is_never_upgraded_by_presence(self) -> None:
        set_mode_override("unattended")
        self._stamp_keystroke(ago=dt.timedelta(minutes=1))
        resolved = resolve_active_mode()
        assert resolved.source == "override"
        assert resolved.defers_questions is True

    def test_presence_insensitive_mode_holds_under_a_keystroke(self) -> None:
        self.away_sensitive.presence_sensitive = False
        self.away_sensitive.save()
        self._stamp_keystroke(ago=dt.timedelta(minutes=1))
        resolved = resolve_active_mode()
        assert resolved.source == "default"
        assert resolved.defers_questions is True


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestReturnToReachableDrain(_TmpStateMixin):
    def setUp(self) -> None:
        super().setUp()
        Mode.objects.create(name="engaged", entries={}, defers_questions=False)
        Mode.objects.create(name="offline", entries={}, defers_questions=True, pauses_self_pump=True)

    def test_returning_from_deferring_mode_drains_the_backlog(self) -> None:
        set_mode_override("offline")
        with mock.patch(_DRAIN) as drain:
            set_mode_override("engaged", user_id="U1", overlay="ov")
        drain.assert_called_once_with(user_id="U1", overlay="ov")

    def test_clearing_to_reachable_drains(self) -> None:
        set_mode_override("offline")
        ConfigSetting.objects.set_value("default_mode", "engaged")
        with mock.patch(_DRAIN) as drain:
            clear_mode_override()
        drain.assert_called_once()

    def test_no_drain_when_staying_deferring(self) -> None:
        set_mode_override("offline")
        with mock.patch(_DRAIN) as drain:
            set_mode_override("offline")
        drain.assert_not_called()
