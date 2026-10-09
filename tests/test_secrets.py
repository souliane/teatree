"""Tests for ``teatree.utils.secrets`` — the ``pass`` password-store readers and writers."""

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from teatree.utils.run import CommandFailedError
from teatree.utils.secrets import (
    KEYRING_READ_TIMEOUT_ENV_VAR,
    KEYRING_READ_TIMEOUT_SECONDS,
    SecretNotFoundError,
    SecretStoreError,
    keyring_read_timeout_seconds,
    read_pass,
    read_pass_required,
    remove_pass,
    write_pass,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_PASS_ABSENT_RC = 1
_GPG_FAILED_RC = 2


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["pass", "show", "any/key"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _bounded(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> "Callable[..., subprocess.CompletedProcess[str]]":
    """Stand in for ``run_bounded_group``, whose contract is to RAISE on a non-zero exit."""

    def call(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if returncode != 0:
            raise CommandFailedError(cmd, returncode, stdout, stderr)
        return _completed(returncode, stdout, stderr)

    return call


class TestReadPass:
    """Behaviour of the ``read_pass`` helper."""

    def test_returns_first_line_of_pass_output(self) -> None:
        """Successful invocation returns the first line, stripped."""
        with patch("teatree.utils.secrets.run_bounded_group", side_effect=_bounded(stdout="s3cret-value\nmetadata\n")):
            assert read_pass("my/secret") == "s3cret-value"

    def test_returns_empty_string_when_the_entry_is_absent(self) -> None:
        """``pass show`` exits 1 for "is not in the password store" — a genuine empty."""
        with patch(
            "teatree.utils.secrets.run_bounded_group", side_effect=_bounded(_PASS_ABSENT_RC, stderr="not found")
        ):
            assert read_pass("missing/key") == ""

    def test_returns_empty_string_when_pass_not_installed(self) -> None:
        """When the ``pass`` binary is absent, return empty string."""
        with patch("teatree.utils.secrets.run_bounded_group", side_effect=FileNotFoundError("pass")):
            assert read_pass("any/key") == ""

    def test_returns_empty_string_on_empty_output(self) -> None:
        """When ``pass`` returns blank output, return empty string."""
        with patch("teatree.utils.secrets.run_bounded_group", side_effect=_bounded(stdout="   \n")):
            assert read_pass("empty/key") == ""


class TestReadPassFailsLoud:
    """A read that FAILED is never laundered into an absent secret (`/t3:rules`)."""

    def test_undecryptable_entry_raises_instead_of_returning_empty(self) -> None:
        """A wedged gpg makes ``pass`` exit 2 — the read did not happen, entry unknown."""
        broken = _bounded(_GPG_FAILED_RC, stderr="gpg: No Keybox daemon running\n")
        with (
            patch("teatree.utils.secrets.run_bounded_group", side_effect=broken),
            pytest.raises(SecretStoreError) as err,
        ):
            read_pass("slack/bot-token")

        assert "slack/bot-token" in str(err.value)
        assert "No Keybox daemon running" in str(err.value)

    def test_the_failure_claims_nothing_about_whether_the_entry_exists(self) -> None:
        """Gpg exits 2 when its keybox daemon will not start — BEFORE any entry is reached.

        Saying "the entry exists but could not be decrypted" sends the reader to check
        the entry, which is the one place the answer is not.
        """
        broken = _bounded(_GPG_FAILED_RC, stderr="gpg: error running '/usr/lib/gnupg/keyboxd': exit status 2\n")
        with (
            patch("teatree.utils.secrets.run_bounded_group", side_effect=broken),
            pytest.raises(SecretStoreError) as err,
        ):
            read_pass("slack/bot-token")

        message = str(err.value)
        assert "the entry exists but could not be decrypted" not in message
        assert "whether the entry exists is unknown" in message

    def test_the_failure_quotes_the_gnupghome_that_separates_the_venues(self, monkeypatch) -> None:
        """The one datum that tells a working host from a container carrying another home."""
        monkeypatch.setenv("GNUPGHOME", "/home/teatree/.gnupg")
        broken = _bounded(_GPG_FAILED_RC, stderr="gpg: decryption failed\n")
        with (
            patch("teatree.utils.secrets.run_bounded_group", side_effect=broken),
            pytest.raises(SecretStoreError) as err,
        ):
            read_pass("slack/bot-token")

        assert "GNUPGHOME=/home/teatree/.gnupg" in str(err.value)

    def test_timeout_raises_naming_the_deadline(self) -> None:
        """An agent that never answers is a failure, not an unbounded wait."""
        timed_out = subprocess.TimeoutExpired(cmd=["pass", "show", "gitlab/pat"], timeout=KEYRING_READ_TIMEOUT_SECONDS)
        with (
            patch("teatree.utils.secrets.run_bounded_group", side_effect=timed_out),
            pytest.raises(SecretStoreError) as err,
        ):
            read_pass("gitlab/pat")

        assert "gitlab/pat" in str(err.value)
        assert "timed out" in str(err.value)

    def test_every_read_carries_the_deadline(self) -> None:
        """No ``pass`` invocation is ever issued without one."""
        with patch("teatree.utils.secrets.run_bounded_group", side_effect=_bounded(stdout="v\n")) as run:
            read_pass("any/key")

        assert run.call_args.kwargs["timeout"] == KEYRING_READ_TIMEOUT_SECONDS


class TestReadPassRequired:
    """The fail-loud reader keeps absent, empty, and unreadable distinguishable."""

    def test_absent_entry_names_the_insert_remedy(self) -> None:
        absent = _bounded(_PASS_ABSENT_RC)
        with (
            patch("teatree.utils.secrets.run_bounded_group", side_effect=absent),
            pytest.raises(SecretNotFoundError) as err,
        ):
            read_pass_required("missing/key")

        assert "no entry" in str(err.value)

    def test_blank_entry_is_reported_as_empty_not_absent(self) -> None:
        blank = _bounded(stdout="  \n")
        with (
            patch("teatree.utils.secrets.run_bounded_group", side_effect=blank),
            pytest.raises(SecretNotFoundError) as err,
        ):
            read_pass_required("blank/key")

        assert "is empty" in str(err.value)

    def test_missing_pass_binary_names_the_install_remedy(self) -> None:
        with (
            patch("teatree.utils.secrets.run_bounded_group", side_effect=FileNotFoundError("pass")),
            pytest.raises(SecretNotFoundError) as err,
        ):
            read_pass_required("any/key")

        assert "not installed" in str(err.value)

    def test_unreadable_entry_is_not_reported_as_a_missing_one(self) -> None:
        """The misdiagnosis this guards: "run `pass insert`" for a wedged keyring."""
        broken = _bounded(_GPG_FAILED_RC, stderr="gpg: decryption failed\n")
        with patch("teatree.utils.secrets.run_bounded_group", side_effect=broken), pytest.raises(SecretStoreError):
            read_pass_required("slack/bot-token")


class TestWrites:
    """Writes carry the same deadline; a refused write stays distinguishable from a hung one."""

    def test_write_returns_false_when_pass_refuses(self) -> None:
        with patch("teatree.utils.secrets.run_bounded_group", side_effect=_bounded(_PASS_ABSENT_RC)):
            assert write_pass("any/key", "v") is False

    def test_write_raises_when_it_hangs(self) -> None:
        timed_out = subprocess.TimeoutExpired(cmd=["pass", "insert"], timeout=KEYRING_READ_TIMEOUT_SECONDS)
        with patch("teatree.utils.secrets.run_bounded_group", side_effect=timed_out), pytest.raises(SecretStoreError):
            write_pass("any/key", "v")

    def test_remove_raises_when_it_hangs(self) -> None:
        timed_out = subprocess.TimeoutExpired(cmd=["pass", "rm"], timeout=KEYRING_READ_TIMEOUT_SECONDS)
        with patch("teatree.utils.secrets.run_bounded_group", side_effect=timed_out), pytest.raises(SecretStoreError):
            remove_pass("any/key")


class TestKeyringReadTimeoutSeconds:
    """The deadline is widenable for a slow smartcard, never removable."""

    def test_defaults_to_the_module_constant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(KEYRING_READ_TIMEOUT_ENV_VAR, raising=False)
        assert keyring_read_timeout_seconds() == KEYRING_READ_TIMEOUT_SECONDS

    @pytest.mark.parametrize("raw", ["", "not-a-number", "0", "-5"])
    def test_an_unusable_override_falls_back_to_the_default(self, raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(KEYRING_READ_TIMEOUT_ENV_VAR, raw)
        assert keyring_read_timeout_seconds() == KEYRING_READ_TIMEOUT_SECONDS

    def test_a_positive_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(KEYRING_READ_TIMEOUT_ENV_VAR, "90")
        assert keyring_read_timeout_seconds() == pytest.approx(90.0)
