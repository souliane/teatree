"""Behaviour of the GitLab MR discussion-thread / approvals-payload helpers."""

from teatree.backends.gitlab.discussions import (
    _count_unresolved_resolvable_threads,
    _note_author,
    _read_int,
    thread_opened_solely_by,
)


class TestReadInt:
    def test_reads_plain_int(self) -> None:
        assert _read_int({"count": 3}, "count") == 3

    def test_reads_string_encoded_int(self) -> None:
        assert _read_int({"count": "5"}, "count") == 5

    def test_missing_key_is_sentinel(self) -> None:
        assert _read_int({}, "count") == -1

    def test_bool_is_not_an_int(self) -> None:
        assert _read_int({"count": True}, "count") == -1

    def test_unparsable_string_is_sentinel(self) -> None:
        assert _read_int({"count": "nan"}, "count") == -1


class TestNoteAuthor:
    def test_reads_username(self) -> None:
        assert _note_author({"author": {"username": "bot"}}) == "bot"

    def test_null_author_is_blank(self) -> None:
        assert _note_author({"author": None}) == ""

    def test_missing_author_is_blank(self) -> None:
        assert _note_author({}) == ""

    def test_non_string_username_is_blank(self) -> None:
        assert _note_author({"author": {"username": 123}}) == ""


class TestThreadOpenedSolelyBy:
    def test_true_when_only_bot_authored(self) -> None:
        thread = {"notes": [{"author": {"username": "bot"}}, {"author": {"username": "bot"}}]}
        assert thread_opened_solely_by(thread, "bot") is True

    def test_bot_note_plus_system_note_still_counts_as_solely_bot(self) -> None:
        thread = {"notes": [{"author": {"username": "bot"}}, {"author": None}]}
        assert thread_opened_solely_by(thread, "bot") is True

    def test_false_when_a_human_replied(self) -> None:
        thread = {"notes": [{"author": {"username": "bot"}}, {"author": {"username": "alice"}}]}
        assert thread_opened_solely_by(thread, "bot") is False

    def test_false_when_bot_did_not_open(self) -> None:
        thread = {"notes": [{"author": {"username": "alice"}}, {"author": {"username": "bot"}}]}
        assert thread_opened_solely_by(thread, "bot") is False

    def test_blank_author_never_eats_threads(self) -> None:
        assert thread_opened_solely_by({"notes": [{"author": {"username": "bot"}}]}, "") is False

    def test_empty_notes_is_false(self) -> None:
        assert thread_opened_solely_by({"notes": []}, "bot") is False


class TestCountUnresolvedResolvableThreads:
    def _discussions(self) -> list[dict[str, object]]:
        return [
            {"notes": [{"resolvable": True, "resolved": False, "author": {"username": "bot"}}]},
            {"notes": [{"resolvable": True, "resolved": False, "author": {"username": "alice"}}]},
            {"notes": [{"resolvable": True, "resolved": True, "author": {"username": "alice"}}]},
            {"notes": [{"resolvable": False, "author": {"username": "system"}}]},
        ]

    def test_counts_open_resolvable_threads(self) -> None:
        assert _count_unresolved_resolvable_threads(self._discussions()) == 2

    def test_default_ignore_author_is_byte_identical(self) -> None:
        assert _count_unresolved_resolvable_threads(self._discussions(), ignore_author="") == 2

    def test_ignore_author_excludes_stale_bot_threads(self) -> None:
        assert _count_unresolved_resolvable_threads(self._discussions(), ignore_author="bot") == 1
