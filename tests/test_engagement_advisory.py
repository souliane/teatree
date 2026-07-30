# test-path: cross-cutting
"""The SessionStart engagement advisory and its ``autoload`` resolution reason (#3499).

The advisory used to be unconditional, so an install whose settings store could not be
read told the operator to set a flag they had already set. These tests pin the branch:
an UNREADABLE store gets its own text naming the breakage, everything else keeps the
original how-to-start line, and fail-closed is unchanged throughout.
"""

import pytest

from hooks.scripts import teatree_settings
from hooks.scripts.engagement_advisory import (
    AUTOLOAD_FROM_DB,
    AUTOLOAD_FROM_DEFAULT,
    AUTOLOAD_FROM_ENV,
    AUTOLOAD_UNREADABLE,
    TEATREE_NOT_ACTIVE_ADVISORY,
    TEATREE_SETTINGS_UNREADABLE_ADVISORY,
    autoload_resolution,
    session_start_advisory,
)
from hooks.scripts.teatree_settings import COLD_READ_OK, COLD_READ_UNREADABLE, autoload_enabled

_STORE_EXPLODED = "store exploded"


@pytest.fixture(autouse=True)
def _no_env_shortcircuit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ``T3_AUTOLOAD`` so the DB path is exercised unless a test sets it back."""
    monkeypatch.delenv("T3_AUTOLOAD", raising=False)


def _stub_store(monkeypatch: pytest.MonkeyPatch, *, value: object, status: str) -> None:
    """Force what the cold reader reports, for both readers under test."""
    monkeypatch.setattr(teatree_settings, "read_cold_setting_status", lambda _name: (value, status))


class TestAutoloadResolution:
    def test_db_true_is_reported_as_db_sourced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_store(monkeypatch, value=True, status=COLD_READ_OK)
        assert autoload_resolution() == (True, AUTOLOAD_FROM_DB)

    def test_db_false_is_reported_as_db_sourced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_store(monkeypatch, value=False, status=COLD_READ_OK)
        assert autoload_resolution() == (False, AUTOLOAD_FROM_DB)

    def test_absent_row_is_the_fail_closed_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_store(monkeypatch, value=None, status=COLD_READ_OK)
        assert autoload_resolution() == (False, AUTOLOAD_FROM_DEFAULT)

    def test_unreadable_store_is_distinguished_from_never_opted_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole point of #3499: both resolve OFF, but only one is a broken install."""
        _stub_store(monkeypatch, value=None, status=COLD_READ_UNREADABLE)
        assert autoload_resolution() == (False, AUTOLOAD_UNREADABLE)

    def test_env_wins_and_is_reported_as_env_sourced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_store(monkeypatch, value=None, status=COLD_READ_UNREADABLE)
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        assert autoload_resolution() == (True, AUTOLOAD_FROM_ENV)


class TestAutoloadResolutionMatchesEnabled:
    """``autoload_resolution`` and ``autoload_enabled`` encode the same order separately.

    ``autoload_enabled`` deliberately does not delegate (it must stay importable under
    the bare ``teatree_settings`` identity), so this pins the duplication equal.
    """

    @pytest.mark.parametrize(
        ("value", "status"),
        [
            (True, COLD_READ_OK),
            (False, COLD_READ_OK),
            (None, COLD_READ_OK),
            (None, COLD_READ_UNREADABLE),
            ("not-a-bool", COLD_READ_OK),
        ],
    )
    def test_verdicts_agree(self, monkeypatch: pytest.MonkeyPatch, value: object, status: str) -> None:
        _stub_store(monkeypatch, value=value, status=status)
        assert autoload_resolution()[0] is autoload_enabled()

    @pytest.mark.parametrize("env", ["1", "true", "yes", "on", "0", "false", "nonsense"])
    def test_env_verdicts_agree(self, monkeypatch: pytest.MonkeyPatch, env: str) -> None:
        _stub_store(monkeypatch, value=None, status=COLD_READ_OK)
        monkeypatch.setenv("T3_AUTOLOAD", env)
        assert autoload_resolution()[0] is autoload_enabled()


class TestSessionStartAdvisory:
    def test_unreadable_store_names_the_breakage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_store(monkeypatch, value=None, status=COLD_READ_UNREADABLE)
        assert session_start_advisory() == TEATREE_SETTINGS_UNREADABLE_ADVISORY

    def test_unreadable_store_never_tells_the_operator_to_set_autoload(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The reported bug: advice to set a flag that may already be set, and unreadable anyway."""
        _stub_store(monkeypatch, value=None, status=COLD_READ_UNREADABLE)
        assert "config_setting set autoload" not in session_start_advisory()

    def test_readable_store_keeps_the_how_to_start_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_store(monkeypatch, value=None, status=COLD_READ_OK)
        assert session_start_advisory() == TEATREE_NOT_ACTIVE_ADVISORY

    def test_a_crashing_reader_degrades_to_the_original_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Crash-proof: the advisory must never be what breaks SessionStart."""

        def _boom(_name: str) -> tuple[object, str]:
            raise RuntimeError(_STORE_EXPLODED)

        monkeypatch.setattr(teatree_settings, "read_cold_setting_status", _boom)
        assert session_start_advisory() == TEATREE_NOT_ACTIVE_ADVISORY
