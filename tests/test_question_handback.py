# test-path: cross-cutting
# Exercises hooks/scripts/question_handback.py (no src/teatree mirror); delivery through the router is in
# tests/test_hook_router_cold_paths.py.
import errno
import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.question_handback import handle_hand_back_answers
from teatree import answer_handback


@pytest.fixture(autouse=True)
def _data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


def test_every_waiting_answer_rides_one_block(capsys: pytest.CaptureFixture[str]) -> None:
    answer_handback.post(session_id="s-1", question_id=7, answer="use postgres-1")
    answer_handback.post(session_id="s-1", question_id=8, answer="ship it")

    assert handle_hand_back_answers({"session_id": "s-1"}) is True

    reason = json.loads(capsys.readouterr().out)["reason"]
    assert reason.splitlines() == [
        'Your AskUserQuestion #7 was answered by the user: "use postgres-1". Apply it now.',
        'Your AskUserQuestion #8 was answered by the user: "ship it". Apply it now.',
    ]


@pytest.mark.parametrize("session_id", ["", "..", "../s-1"])
def test_a_session_id_naming_no_mailbox_delivers_nothing(session_id: str, capsys: pytest.CaptureFixture[str]) -> None:
    answer_handback.post(session_id="s-1", question_id=7, answer="use postgres-1")

    assert handle_hand_back_answers({"session_id": session_id}) is None
    assert capsys.readouterr().out == ""
    assert len(answer_handback.collect("s-1")) == 1


def test_a_resumed_session_gets_its_waiting_answer_at_session_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(router, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.delenv("T3_AUTOLOAD", raising=False)
    answer_handback.post(session_id="s-resumed", question_id=7, answer="use postgres-1")

    router.handle_session_start_bootstrap({"session_id": "s-resumed", "source": "resume"})

    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert 'Your AskUserQuestion #7 was answered by the user: "use postgres-1". Apply it now.' in context
    assert answer_handback.collect("s-resumed") == []


def test_an_unreadable_mailbox_never_costs_a_compacted_session_its_recovery_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(router, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(router, "_TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.delenv("T3_AUTOLOAD", raising=False)
    router.STATE_DIR.mkdir()
    (router.STATE_DIR / f"{router._T3_TEMP_PREFIX}s-compacted-precompact.md").write_text("half-done", encoding="utf-8")

    def refused(session_id: str) -> list[answer_handback.HandedBackAnswer]:
        raise PermissionError(errno.EACCES, "mailbox refused", session_id)

    monkeypatch.setattr(answer_handback, "collect", refused)

    router.handle_session_start_bootstrap({"session_id": "s-compacted", "source": "compact"})

    captured = capsys.readouterr()
    assert "half-done" in json.loads(captured.out)["hookSpecificOutput"]["additionalContext"]
    assert "mailbox refused" in captured.err
