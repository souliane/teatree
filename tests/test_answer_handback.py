import logging
import tempfile
from pathlib import Path

import pytest

from teatree.answer_handback import HandedBackAnswer, collect, mailbox, post
from tests._unreadable_file import skip_if_root


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"XDG_DATA_HOME": str(tmp_path / "data")}


class TestAnAnswerReachesOnlyTheSessionThatAsked:
    def test_the_asking_session_collects_it(self, env: dict[str, str]) -> None:
        post(session_id="s-1", question_id=7, answer="use postgres-1", env=env)

        assert collect("s-1", env=env) == [{"id": 7, "answer": "use postgres-1"}]

    def test_another_session_never_sees_it(self, env: dict[str, str]) -> None:
        post(session_id="s-1", question_id=7, answer="use postgres-1", env=env)

        assert collect("s-2", env=env) == []
        assert len(collect("s-1", env=env)) == 1

    def test_answers_come_back_oldest_question_first(self, env: dict[str, str]) -> None:
        post(session_id="s-1", question_id=10, answer="second", env=env)
        post(session_id="s-1", question_id=9, answer="first", env=env)

        assert [answer["id"] for answer in collect("s-1", env=env)] == [9, 10]


class TestAnAnswerIsDeliveredAtMostOnce:
    def test_a_second_collect_finds_nothing_and_the_mailbox_is_left_empty(self, env: dict[str, str]) -> None:
        post(session_id="s-1", question_id=7, answer="use postgres-1", env=env)
        collect("s-1", env=env)

        assert collect("s-1", env=env) == []
        box = mailbox("s-1", env=env)
        assert box is not None
        assert not box.exists()

    def test_a_post_racing_the_collector_that_empties_the_mailbox_still_lands(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        box = mailbox("s-1", env=env)
        assert box is not None
        mkstemp = tempfile.mkstemp
        raced: list[bool] = []

        def the_collector_removes_the_box_first(**_: object) -> tuple[int, str]:
            if not raced:
                raced.append(True)
                box.rmdir()
            return mkstemp(prefix=".", suffix=".tmp", dir=box)

        monkeypatch.setattr("teatree.answer_handback.tempfile.mkstemp", the_collector_removes_the_box_first)

        post(session_id="s-1", question_id=7, answer="use postgres-1", env=env)

        assert raced
        assert collect("s-1", env=env) == [{"id": 7, "answer": "use postgres-1"}]

    def test_a_collector_racing_mid_read_gets_nothing(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        post(session_id="s-1", question_id=7, answer="use postgres-1", env=env)
        read_text = Path.read_text
        racer: list[HandedBackAnswer] = []
        raced: list[bool] = []

        def another_stop_collects_after_this_read(path: Path, encoding: str) -> str:
            content = read_text(path, encoding=encoding)
            if not raced:
                raced.append(True)
                racer.extend(collect("s-1", env=env))
            return content

        monkeypatch.setattr(Path, "read_text", another_stop_collects_after_this_read)

        delivered = collect("s-1", env=env)

        assert raced
        assert delivered + racer == [{"id": 7, "answer": "use postgres-1"}]


class TestASessionIdNamesOneMailboxOrNone:
    @pytest.mark.parametrize("session_id", ["", ".", "..", "../s-1", "s-1/../../etc", ".hidden"])
    def test_an_id_that_is_not_one_plain_segment_has_no_mailbox(self, env: dict[str, str], session_id: str) -> None:
        assert mailbox(session_id, env=env) is None
        assert collect(session_id, env=env) == []

    def test_posting_to_it_is_refused(self, env: dict[str, str]) -> None:
        with pytest.raises(ValueError, match="names no mailbox"):
            post(session_id="../s-1", question_id=7, answer="x", env=env)


@skip_if_root
class TestAMailboxThatCannotBeUsedIsReported:
    def test_a_claim_refused_for_any_reason_but_the_race_is_logged_and_the_answer_kept(
        self, env: dict[str, str], caplog: pytest.LogCaptureFixture
    ) -> None:
        post(session_id="s-1", question_id=7, answer="use postgres-1", env=env)
        box = mailbox("s-1", env=env)
        assert box is not None
        box.chmod(0o500)
        try:
            with caplog.at_level(logging.WARNING, logger="teatree.answer_handback"):
                assert collect("s-1", env=env) == []
        finally:
            box.chmod(0o755)

        assert "Could not claim the handed-back answer" in caplog.text
        assert collect("s-1", env=env) == [{"id": 7, "answer": "use postgres-1"}]

    def test_an_unlistable_mailbox_is_logged(self, env: dict[str, str], caplog: pytest.LogCaptureFixture) -> None:
        post(session_id="s-1", question_id=7, answer="use postgres-1", env=env)
        box = mailbox("s-1", env=env)
        assert box is not None
        box.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger="teatree.answer_handback"):
                assert collect("s-1", env=env) == []
        finally:
            box.chmod(0o755)

        assert "Could not list the answer mailbox" in caplog.text
