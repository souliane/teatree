"""The retired directive reader has no dispatch or tool-policy registration."""

from pathlib import Path

from teatree.core.modelkit.phase_tools import _TOOLS_BY_PHASE

_ROOT = Path(__file__).resolve().parents[3] / "src" / "teatree"


def test_directive_reader_phase_is_not_registered() -> None:
    assert "directive_reading" not in _TOOLS_BY_PHASE


def test_directive_reader_profile_is_absent() -> None:
    assert not (_ROOT / "agents" / "reader_profile.py").exists()


def test_active_dispatch_code_has_no_directive_reader_branch() -> None:
    for relative in (
        "agents/runner.py",
        "agents/_runner_options.py",
        "agents/runner_preparation.py",
        "core/modelkit/phase_tools.py",
    ):
        assert "directive_reading" not in (_ROOT / relative).read_text(encoding="utf-8")
