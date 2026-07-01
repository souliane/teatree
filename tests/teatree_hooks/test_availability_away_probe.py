"""Stdlib away-probe predicates for the bare-``python3`` hooks (#2544, #2559).

The hook interpreter cannot import teatree, so it reads the resolved mode by
subprocessing ``t3 <overlay> availability show`` and parsing the ``mode=…``
token. #2544 splits the single ``away`` read into two orthogonal predicates so
``autonomous_away`` can defer questions without pausing the self-pump.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_HOOK_DIR = Path(__file__).resolve().parents[2] / "hooks" / "scripts"
if str(_HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOK_DIR))

import availability_away_probe as probe  # noqa: E402

from teatree.core import availability as core  # noqa: E402


def _stub_show(monkeypatch: pytest.MonkeyPatch, stdout: str, *, returncode: int = 0) -> None:
    monkeypatch.setattr(probe, "shutil", SimpleNamespace(which=lambda _n: "/usr/local/bin/t3"))

    def _run(_argv: list[str], *_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(probe, "subprocess", SimpleNamespace(run=_run, TimeoutExpired=subprocess.TimeoutExpired))


class TestModeToken:
    @pytest.mark.parametrize("mode", ["present", "away", "autonomous_away"])
    def test_parses_each_mode(self, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
        _stub_show(monkeypatch, f"availability: mode={mode} source=override")
        assert probe.resolved_mode_token() == mode

    def test_absent_t3_yields_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(probe, "shutil", SimpleNamespace(which=lambda _n: None))
        assert probe.resolved_mode_token() == ""

    def test_error_exit_yields_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_show(monkeypatch, "", returncode=1)
        assert probe.resolved_mode_token() == ""


class TestDefersQuestions:
    def test_present_does_not_defer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_show(monkeypatch, "availability: mode=present source=default")
        assert probe.resolved_defers_questions() is False

    def test_away_defers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_show(monkeypatch, "availability: mode=away source=override")
        assert probe.resolved_defers_questions() is True

    def test_autonomous_away_defers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_show(monkeypatch, "availability: mode=autonomous_away source=override")
        assert probe.resolved_defers_questions() is True


class TestPausesSelfPump:
    def test_present_does_not_pause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_show(monkeypatch, "availability: mode=present source=default")
        assert probe.resolved_pauses_self_pump() is False

    def test_away_pauses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_show(monkeypatch, "availability: mode=away source=override")
        assert probe.resolved_pauses_self_pump() is True

    def test_autonomous_away_does_not_pause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # #2544: the factory keeps self-pumping under autonomous-away.
        _stub_show(monkeypatch, "availability: mode=autonomous_away source=override")
        assert probe.resolved_pauses_self_pump() is False


class TestStdlibParityWithCore:
    """The stdlib mode-token sets must mirror ``teatree.core.availability``."""

    def test_deferring_tokens_match_core(self) -> None:
        assert probe._DEFERRING_MODE_TOKENS == core._DEFERRING_MODES

    def test_away_token_matches_core(self) -> None:
        assert probe._MODE_AWAY == core.MODE_AWAY
        assert probe._MODE_AUTONOMOUS_AWAY == core.MODE_AUTONOMOUS_AWAY
