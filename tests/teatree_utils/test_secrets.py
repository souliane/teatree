"""``pass`` password store helpers — write_pass.

Note: ``read_pass`` is globally patched by the autouse ``_clear_backend_caches``
fixture in ``tests/conftest.py`` (to block real ``gpg``/``pass`` calls), so we
only exercise ``write_pass`` here, which is what's currently uncovered.
"""

from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from teatree.utils import secrets
from teatree.utils.run import CommandFailedError


class TestWritePass:
    def test_returns_true_on_successful_insert(self) -> None:
        result = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("teatree.utils.secrets.run_checked", return_value=result) as mock:
            assert secrets.write_pass("acme/token", "abc") is True
        called = mock.call_args
        assert called.args[0] == ["pass", "insert", "--multiline", "--force", "acme/token"]
        assert called.kwargs["stdin_text"] == "abc"

    def test_returns_false_when_pass_command_fails(self) -> None:
        with patch("teatree.utils.secrets.run_checked", side_effect=CommandFailedError(["pass"], 1, "", "boom")):
            assert secrets.write_pass("acme/token", "abc") is False

    def test_returns_false_when_pass_not_installed(self) -> None:
        with patch("teatree.utils.secrets.run_checked", side_effect=FileNotFoundError):
            assert secrets.write_pass("acme/token", "abc") is False


class TestRemovePass:
    def test_returns_true_when_remove_succeeds(self) -> None:
        result = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("teatree.utils.secrets.run_checked", return_value=result) as mock:
            assert secrets.remove_pass("acme/token") is True
        assert mock.call_args.args[0] == ["pass", "rm", "--force", "acme/token"]

    def test_returns_false_when_pass_command_fails(self) -> None:
        with patch("teatree.utils.secrets.run_checked", side_effect=CommandFailedError(["pass"], 1, "", "boom")):
            assert secrets.remove_pass("acme/token") is False

    def test_returns_false_when_pass_not_installed(self) -> None:
        with patch("teatree.utils.secrets.run_checked", side_effect=FileNotFoundError):
            assert secrets.remove_pass("acme/token") is False


class TestPassEntryExists:
    def test_returns_true_when_pass_resolves_value(self) -> None:
        with patch("teatree.utils.secrets.read_pass", return_value="value"):
            assert secrets.pass_entry_exists("acme/token") is True

    def test_returns_false_when_pass_returns_empty(self) -> None:
        with patch("teatree.utils.secrets.read_pass", return_value=""):
            assert secrets.pass_entry_exists("acme/token") is False


class TestReadPassRequired:
    """The fail-loud reader: raise (naming the key) on absent/empty/missing-tool."""

    def test_returns_value_on_happy_path(self) -> None:
        result = CompletedProcess(args=[], returncode=0, stdout="s3cret\nother\n", stderr="")
        with patch("teatree.utils.secrets.run_checked", return_value=result):
            assert secrets.read_pass_required("acme/token") == "s3cret"

    def test_raises_and_names_key_when_entry_absent(self) -> None:
        with (
            patch("teatree.utils.secrets.run_checked", side_effect=CommandFailedError(["pass"], 1, "", "nope")),
            pytest.raises(secrets.SecretNotFoundError, match="acme/token"),
        ):
            secrets.read_pass_required("acme/token")

    def test_absent_message_hints_pass_insert(self) -> None:
        with (
            patch("teatree.utils.secrets.run_checked", side_effect=CommandFailedError(["pass"], 1, "", "")),
            pytest.raises(secrets.SecretNotFoundError, match="pass insert acme/token"),
        ):
            secrets.read_pass_required("acme/token")

    def test_raises_when_entry_is_empty(self) -> None:
        result = CompletedProcess(args=[], returncode=0, stdout="\n", stderr="")
        with (
            patch("teatree.utils.secrets.run_checked", return_value=result),
            pytest.raises(secrets.SecretNotFoundError, match="empty"),
        ):
            secrets.read_pass_required("acme/token")

    def test_missing_tool_message_differs_from_absent_entry(self) -> None:
        with (
            patch("teatree.utils.secrets.run_checked", side_effect=FileNotFoundError),
            pytest.raises(secrets.SecretNotFoundError, match="not installed"),
        ):
            secrets.read_pass_required("acme/token")


class TestReadPassOrDefault:
    """The warn-on-default reader: value when present, default + WARNING when not."""

    def test_returns_secret_when_present(self) -> None:
        with patch("teatree.utils.secrets.read_pass", return_value="live"):
            assert secrets.read_pass_or_default("acme/token", "fallback") == "live"

    def test_returns_default_and_warns_when_absent(self, caplog: pytest.LogCaptureFixture) -> None:
        with patch("teatree.utils.secrets.read_pass", return_value=""), caplog.at_level("WARNING"):
            assert secrets.read_pass_or_default("acme/token", "fallback") == "fallback"
        assert "acme/token" in caplog.text


class TestWritePassWithBackup:
    """The shared safe-overwrite primitive: back up the prior value, or refuse to clobber."""

    def test_writes_straight_through_when_no_prior_value_exists(self) -> None:
        written: list[tuple[str, str]] = []
        with (
            patch("teatree.utils.secrets.read_pass", return_value=""),
            patch("teatree.utils.secrets.write_pass", side_effect=lambda k, v: written.append((k, v)) or True),
        ):
            backup = secrets.write_pass_with_backup("acme/token", "abc", echo=lambda _: None)

        assert backup == ""
        assert written == [("acme/token", "abc")]

    def test_copies_the_prior_value_to_a_stamped_backup_before_overwriting(self) -> None:
        written: list[tuple[str, str]] = []
        with (
            patch("teatree.utils.secrets.read_pass", return_value="previous"),
            patch("teatree.utils.secrets.write_pass", side_effect=lambda k, v: written.append((k, v)) or True),
        ):
            backup = secrets.write_pass_with_backup("acme/token", "abc", echo=lambda _: None)

        assert backup.startswith("acme/token.bak-")
        assert written == [(backup, "previous"), ("acme/token", "abc")], "the backup must precede the overwrite"

    def test_announces_the_backup_key_it_wrote(self) -> None:
        said: list[str] = []
        with (
            patch("teatree.utils.secrets.read_pass", return_value="previous"),
            patch("teatree.utils.secrets.write_pass", return_value=True),
        ):
            backup = secrets.write_pass_with_backup("acme/token", "abc", echo=said.append)

        assert any(backup in line for line in said)

    def test_a_failed_backup_raises_and_never_overwrites_the_original(self) -> None:
        written: list[tuple[str, str]] = []

        def refuse_backup(key: str, value: str) -> bool:
            written.append((key, value))
            return ".bak-" not in key

        with (
            patch("teatree.utils.secrets.read_pass", return_value="previous"),
            patch("teatree.utils.secrets.write_pass", side_effect=refuse_backup),
            pytest.raises(secrets.SecretStoreError, match="acme/token"),
        ):
            secrets.write_pass_with_backup("acme/token", "abc", echo=lambda _: None)

        assert all(".bak-" in key for key, _ in written), "the original must stay untouched when the backup fails"

    def test_a_failed_insert_raises_rather_than_reporting_a_write_that_did_not_happen(self) -> None:
        with (
            patch("teatree.utils.secrets.read_pass", return_value=""),
            patch("teatree.utils.secrets.write_pass", return_value=False),
            pytest.raises(secrets.SecretStoreError, match="acme/token"),
        ):
            secrets.write_pass_with_backup("acme/token", "abc", echo=lambda _: None)

    def test_an_empty_value_is_refused_before_any_write(self) -> None:
        with (
            patch("teatree.utils.secrets.read_pass", return_value="previous"),
            patch("teatree.utils.secrets.write_pass", return_value=True) as write,
            pytest.raises(secrets.SecretStoreError, match="empty"),
        ):
            secrets.write_pass_with_backup("acme/token", "   ", echo=lambda _: None)

        assert write.call_count == 0
