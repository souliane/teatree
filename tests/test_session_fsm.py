"""Tests for session phase state machine with quality gates."""

from pathlib import Path

import pytest
from lib.session_fsm import GateBlockedError, SessionPhase


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "sessions"
    sd.mkdir()
    return sd


@pytest.fixture
def session(session_dir: Path) -> SessionPhase:
    return SessionPhase(session_id="test-123", state_dir=str(session_dir))


class TestInitialState:
    def test_initial_state_is_idle(self, session: SessionPhase) -> None:
        assert session.state == "idle"


class TestHappyPath:
    def test_scope_from_idle(self, session: SessionPhase) -> None:
        session.begin_scoping()
        assert session.state == "scoping"

    def test_code_from_idle(self, session: SessionPhase) -> None:
        session.begin_coding()
        assert session.state == "coding"

    def test_code_from_scoping(self, session: SessionPhase) -> None:
        session.begin_scoping()
        session.begin_coding()
        assert session.state == "coding"

    def test_test_from_coding(self, session: SessionPhase) -> None:
        session.begin_coding()
        session.begin_testing()
        assert session.state == "testing"

    def test_debug_from_coding(self, session: SessionPhase) -> None:
        session.begin_coding()
        session.begin_debugging()
        assert session.state == "debugging"

    def test_code_from_debugging(self, session: SessionPhase) -> None:
        session.begin_coding()
        session.begin_debugging()
        session.begin_coding()
        assert session.state == "coding"

    def test_review_from_testing(self, session: SessionPhase) -> None:
        session.begin_coding()
        session.begin_testing()
        session.begin_reviewing()
        assert session.state == "reviewing"

    def test_ship_from_reviewing(self, session: SessionPhase) -> None:
        session.begin_coding()
        session.begin_testing()
        session.begin_reviewing()
        session.begin_shipping()
        assert session.state == "shipping"

    def test_request_review_from_shipping(self, session: SessionPhase) -> None:
        session.begin_coding()
        session.begin_testing()
        session.begin_reviewing()
        session.begin_shipping()
        session.begin_requesting_review()
        assert session.state == "requesting_review"

    def test_retro_from_any(self, session: SessionPhase) -> None:
        session.begin_coding()
        session.begin_retrospecting()
        assert session.state == "retrospecting"

    def test_retro_from_idle(self, session: SessionPhase) -> None:
        session.begin_retrospecting()
        assert session.state == "retrospecting"


class TestQualityGates:
    """Verify that quality gates block unsafe transitions."""

    def test_cannot_ship_directly_from_coding(self, session: SessionPhase) -> None:
        session.begin_coding()
        with pytest.raises(GateBlockedError, match="testing"):
            session.begin_shipping()

    def test_cannot_review_directly_from_coding(self, session: SessionPhase) -> None:
        session.begin_coding()
        with pytest.raises(GateBlockedError, match="testing"):
            session.begin_reviewing()

    def test_cannot_request_review_without_shipping(self, session: SessionPhase) -> None:
        session.begin_coding()
        session.begin_testing()
        session.begin_reviewing()
        # Skip shipping, try request_review
        with pytest.raises(GateBlockedError, match="shipping"):
            session.begin_requesting_review()

    def test_cannot_ship_from_testing_directly(self, session: SessionPhase) -> None:
        """Must review before shipping."""
        session.begin_coding()
        session.begin_testing()
        with pytest.raises(GateBlockedError, match="reviewing"):
            session.begin_shipping()


class TestForceOverride:
    """Verify that --force bypasses gates."""

    def test_force_ship_from_coding(self, session: SessionPhase) -> None:
        session.begin_coding()
        session.begin_shipping(force=True)
        assert session.state == "shipping"

    def test_force_review_from_coding(self, session: SessionPhase) -> None:
        session.begin_coding()
        session.begin_reviewing(force=True)
        assert session.state == "reviewing"

    def test_force_request_review_without_shipping(self, session: SessionPhase) -> None:
        session.begin_coding()
        session.begin_testing()
        session.begin_reviewing()
        session.begin_requesting_review(force=True)
        assert session.state == "requesting_review"


class TestPhaseHistory:
    """Verify that the session tracks which phases were visited."""

    def test_history_tracks_phases(self, session: SessionPhase) -> None:
        session.begin_coding()
        session.begin_testing()
        session.begin_coding()  # back to coding (fix tests)
        assert session.visited == {"idle", "coding", "testing"}

    def test_has_tested_after_testing(self, session: SessionPhase) -> None:
        session.begin_coding()
        session.begin_testing()
        assert session.has_visited("testing")

    def test_has_not_tested_before_testing(self, session: SessionPhase) -> None:
        session.begin_coding()
        assert not session.has_visited("testing")


class TestPersistence:
    def test_save_and_reload(self, session_dir: Path) -> None:
        s = SessionPhase(session_id="test-456", state_dir=str(session_dir))
        s.begin_coding()
        s.begin_testing()

        # Reload
        s2 = SessionPhase(session_id="test-456", state_dir=str(session_dir))
        assert s2.state == "testing"
        assert s2.has_visited("coding")

    def test_available_transitions(self, session: SessionPhase) -> None:
        session.begin_coding()
        avail = session.available_transitions()
        method_names = [t["method"] for t in avail]
        assert "begin_testing" in method_names
        assert "begin_debugging" in method_names
        # Gated transitions should still appear but with gate info
        assert "begin_shipping" not in method_names or any(
            t.get("blocked") for t in avail if t["method"] == "begin_shipping"
        )
