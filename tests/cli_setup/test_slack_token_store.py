"""Tests for ``slack_token_store`` — validate-before-write + back-up-before-overwrite.

The clobber that destroyed a live xoxp token had two roots: the value was
written without a prefix check, and the prior value was overwritten with no
backup on a non-git store. These tests pin both guards.
"""

from unittest.mock import patch

import pytest

from teatree.cli.slack.token_store import BOT_TOKEN_SLOT, USER_TOKEN_SLOT, SlackTokenWriteError, store_slack_token


class TestValidateBeforeWrite:
    def test_refuses_bot_token_into_user_slot_without_writing(self) -> None:
        with (
            patch("teatree.cli.slack.token_store.read_pass", return_value="xoxp-prioruser"),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True) as write,
            pytest.raises(SlackTokenWriteError, match="must start with 'xoxp-'"),
        ):
            store_slack_token(USER_TOKEN_SLOT, "xoxb-WRONG", echo=lambda _m: None)
        write.assert_not_called()

    def test_refuses_user_token_into_bot_slot_without_writing(self) -> None:
        with (
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True) as write,
            pytest.raises(SlackTokenWriteError, match="must start with 'xoxb-'"),
        ):
            store_slack_token(BOT_TOKEN_SLOT, "xoxp-WRONG", echo=lambda _m: None)
        write.assert_not_called()

    def test_refuses_empty_value(self) -> None:
        with (
            patch("teatree.cli.slack.token_store.write_pass", return_value=True) as write,
            pytest.raises(SlackTokenWriteError, match="empty"),
        ):
            store_slack_token(USER_TOKEN_SLOT, "   ", echo=lambda _m: None)
        write.assert_not_called()

    def test_writes_a_valid_token_when_slot_empty(self) -> None:
        with (
            patch("teatree.cli.slack.token_store.read_pass", return_value=""),
            patch("teatree.cli.slack.token_store.write_pass", return_value=True) as write,
        ):
            backup_key = store_slack_token(USER_TOKEN_SLOT, "xoxp-freshuser", echo=lambda _m: None)
        assert backup_key == ""
        write.assert_called_once_with(USER_TOKEN_SLOT.pass_key, "xoxp-freshuser")


class TestBackupBeforeOverwrite:
    def test_existing_value_backed_up_before_overwrite(self) -> None:
        writes: list[tuple[str, str]] = []
        messages: list[str] = []
        with (
            patch("teatree.cli.slack.token_store.read_pass", return_value="xoxp-prioruser"),
            patch(
                "teatree.cli.slack.token_store.write_pass",
                side_effect=lambda key, value: writes.append((key, value)) or True,
            ),
        ):
            backup_key = store_slack_token(USER_TOKEN_SLOT, "xoxp-freshuser", echo=messages.append)

        assert backup_key.startswith(f"{USER_TOKEN_SLOT.pass_key}.bak-")
        assert (backup_key, "xoxp-prioruser") in writes
        assert (USER_TOKEN_SLOT.pass_key, "xoxp-freshuser") in writes
        assert writes.index((backup_key, "xoxp-prioruser")) < writes.index((USER_TOKEN_SLOT.pass_key, "xoxp-freshuser"))
        assert any("Backed up" in m for m in messages)

    def test_refuses_overwrite_when_backup_write_fails(self) -> None:
        def fake_write(key: str, _value: str) -> bool:
            return not key.startswith(f"{USER_TOKEN_SLOT.pass_key}.bak-")

        with (
            patch("teatree.cli.slack.token_store.read_pass", return_value="xoxp-prioruser"),
            patch("teatree.cli.slack.token_store.write_pass", side_effect=fake_write),
            pytest.raises(SlackTokenWriteError, match="could not back up"),
        ):
            store_slack_token(USER_TOKEN_SLOT, "xoxp-freshuser", echo=lambda _m: None)
