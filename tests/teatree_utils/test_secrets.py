"""``pass`` password store helpers — write_pass.

Note: ``read_pass`` is globally patched by the autouse ``_clear_backend_caches``
fixture in ``tests/conftest.py`` (to block real ``gpg``/``pass`` calls), so we
only exercise ``write_pass`` here, which is what's currently uncovered.
"""

from subprocess import CompletedProcess
from unittest.mock import patch

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
