# test-path: cross-cutting — drives hooks/scripts/session_handover_pickup.py; no src/teatree/ mirror.
"""The DB is the delivery surface; the mirror is read only when the DB is UNREACHABLE (#4194).

The mirror was read whenever the payload came back falsy — which includes the case
where the DB was read perfectly and legitimately returned nothing. That is how one
of four hand-offs arrived: the drain succeeded and returned nothing (all four rows
were addressed to ``"loop-runner"``, claimable by nobody), the mirror's single
``latest.md`` pointer delivered exactly one stale payload, and all four rows stayed
unclaimed.

So the fallback is keyed on the DB being unreachable, never on the answer being
empty — a readable DB that yields nothing delivers nothing, and every mirror
delivery is attributable to a bootstrap rather than to a silent DB miss.
"""

import logging
from pathlib import Path
from unittest import mock

import pytest

import hooks.scripts.session_handover_pickup as pickup
from teatree.core.models import SessionHandover
from teatree.paths import ControlDb

_MIRROR_BODY = "STALE MIRROR PAYLOAD"


@pytest.fixture
def mirror(db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A populated ``latest.md`` mirror the fallback would read."""
    path = tmp_path / "handover" / "latest.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_MIRROR_BODY, encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(pickup, "claim_session_handover_from_file", lambda: (path.read_text(encoding="utf-8"), ""))
    return path


class TestTheDatabaseIsTheDeliverySurface:
    def test_four_pending_rows_are_all_delivered_and_the_mirror_is_untouched(self, mirror: Path) -> None:
        """The measured case: four claimable rows pending, exactly one delivered."""
        for index in range(4):
            SessionHandover.objects.create_handover(
                from_session=f"author-{index}", to_session="", payload=f"payload-{index}"
            )

        directive = pickup.claim_session_handover("the-fresh-session")

        assert directive is not None
        for index in range(4):
            assert f"payload-{index}" in directive, "every pending hand-off is delivered, not one"
        assert _MIRROR_BODY not in directive
        assert SessionHandover.objects.filter(claimed_at__isnull=True).count() == 0
        assert mirror.is_file(), "a DB delivery must not consume the mirror"

    def test_a_readable_but_empty_database_delivers_nothing(self, mirror: Path) -> None:
        """The #4194 mechanism: 'the queue is empty' is an ANSWER, not a reason to read a file."""
        assert pickup.claim_session_handover("the-fresh-session") is None
        assert mirror.read_text(encoding="utf-8") == _MIRROR_BODY, "the mirror must not be consumed"

    def test_an_unimportable_django_still_bootstraps_off_the_mirror(
        self, mirror: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(pickup, "bootstrap_teatree_django", lambda: False)

        with caplog.at_level(logging.WARNING, logger="teatree.hook_router"):
            directive = pickup.claim_session_handover("the-fresh-session")

        assert directive is not None
        assert _MIRROR_BODY in directive
        assert "unreachable" in caplog.text.lower(), "a mirror delivery is always attributable to an unreachable DB"

    def test_a_raising_drain_still_bootstraps_off_the_mirror(
        self, mirror: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(pickup, "bootstrap_teatree_django", lambda: True)

        with (
            mock.patch("teatree.core.handover.claim_handovers", side_effect=RuntimeError("db is locked")),
            caplog.at_level(logging.WARNING, logger="teatree.hook_router"),
        ):
            directive = pickup.claim_session_handover("the-fresh-session")

        assert directive is not None
        assert _MIRROR_BODY in directive
        assert "db is locked" in caplog.text


class TestADivergedControlDbIsLoudRatherThanExtendingTheFallback:
    """Three states, delivery on two: reachable-and-mine, reachable-but-diverged, unreachable.

    ``db_readable`` was two-valued, so a DB that opens but is NOT the DB the hand-off
    was written to delivered nothing with no trace at all — a straight recurrence of
    the #3810 loud-degradation contract this module's docstring claims to honour.

    Extending the mirror fallback to that branch would be WRONG rather than merely
    bigger: an auto-isolated control DB can legitimately BE the delivery DB (a session
    handing off inside worktree W writes to W's DB and the next session in W reads
    it), so reading the SHARED mirror there trades a silent miss for a WRONG delivery.
    """

    def test_a_drain_against_a_diverged_control_db_warns_and_still_delivers_nothing(
        self, mirror: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(ControlDb, "divergence_message", lambda _self, _root: "resolves /a/x vs primary /b/y")

        with caplog.at_level(logging.WARNING, logger="teatree.hook_router"):
            directive = pickup.claim_session_handover("the-fresh-session")

        assert directive is None
        assert mirror.read_text(encoding="utf-8") == _MIRROR_BODY, "a diverged DB must not reach for the mirror"
        assert "resolves /a/x vs primary /b/y" in caplog.text
        assert "the-fresh-session" in caplog.text

    def test_a_drain_against_the_primary_control_db_is_silent(
        self, mirror: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The CONTROL: without it, a probe that warned unconditionally would pass the test above."""
        monkeypatch.setattr(ControlDb, "divergence_message", lambda _self, _root: None)

        with caplog.at_level(logging.WARNING, logger="teatree.hook_router"):
            assert pickup.claim_session_handover("the-fresh-session") is None

        assert caplog.text == "", "an ordinary empty drain against the primary DB has nothing to say"

    def test_a_failing_detector_never_breaks_the_pickup(self, mirror: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_self: ControlDb, _root: Path) -> str:
            msg = "the detector exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(ControlDb, "divergence_message", _boom)

        assert pickup.claim_session_handover("the-fresh-session") is None
