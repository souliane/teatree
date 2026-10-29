# test-path: cross-cutting — drives hooks/scripts/read_dedup.py through the router's per-session state dir.
"""``handle_read_dedup`` warns when a file is re-read unchanged, and records each read in ``<session>.reads``."""

import os
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.read_dedup import handle_read_dedup

SESSION = "sess-reads"


@pytest.fixture(autouse=True)
def state_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path_factory.mktemp("state")
    monkeypatch.setattr(router, "STATE_DIR", state)
    return state


def _read(path: Path, *, session: str = SESSION, tool: str = "Read") -> dict:
    return {"session_id": session, "tool_name": tool, "tool_input": {"file_path": str(path)}}


def test_first_read_is_recorded_without_a_hint(
    tmp_path: Path, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("a", encoding="utf-8")
    handle_read_dedup(_read(target))
    assert capsys.readouterr().out == ""
    assert str(target) in (state_dir / f"{SESSION}.reads").read_text(encoding="utf-8")


def test_an_unchanged_re_read_prints_the_token_savings_hint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("a", encoding="utf-8")
    handle_read_dedup(_read(target))
    handle_read_dedup(_read(target))
    assert f"TOKEN SAVINGS HINT: {target} was already read this session" in capsys.readouterr().out


def test_a_changed_file_is_not_hinted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("a", encoding="utf-8")
    handle_read_dedup(_read(target))
    os.utime(target, (1, 1))
    handle_read_dedup(_read(target))
    assert capsys.readouterr().out == ""


def test_a_vanished_file_records_nothing(tmp_path: Path, state_dir: Path) -> None:
    handle_read_dedup(_read(tmp_path / "gone.txt"))
    assert list(state_dir.glob("*.reads")) == []


@pytest.mark.parametrize(
    "data",
    [
        {"session_id": SESSION, "tool_name": "Bash", "tool_input": {"file_path": "/tmp/x"}},
        {"session_id": "", "tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}},
        {"session_id": SESSION, "tool_name": "Read", "tool_input": {}},
    ],
    ids=["other-tool", "no-session", "no-path"],
)
def test_only_a_read_with_a_session_and_a_path_is_tracked(data: dict, state_dir: Path) -> None:
    handle_read_dedup(data)
    assert list(state_dir.glob("*.reads")) == []


def test_a_malformed_line_in_the_reads_file_is_dropped_on_the_next_read(tmp_path: Path, state_dir: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("a", encoding="utf-8")
    reads = state_dir / f"{SESSION}.reads"
    reads.write_text("no-tab-here\n", encoding="utf-8")
    handle_read_dedup(_read(target))
    assert "no-tab-here" not in reads.read_text(encoding="utf-8")
