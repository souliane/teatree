"""PR-30 bridge exit-code contract.

A bridged child's exit code propagates faithfully as ``SystemExit(returncode)``
with no traceback, so a machine front-end (Pi / CI) can branch on it.
"""

from unittest.mock import patch

import pytest

from teatree.cli import overlay
from teatree.utils.run import CommandFailedError


class TestFaithfulChildExit:
    def test_managepy_reraises_child_code_as_systemexit(self) -> None:
        err = CommandFailedError(["python", "-m", "teatree", "ticket", "transition"], 2, "", "No such option")
        with patch.object(overlay, "run_streamed", side_effect=err), pytest.raises(SystemExit) as exc:
            overlay.managepy(None, "ticket", "transition", "--bad")
        assert exc.value.code == 2

    def test_managepy_core_reraises_child_code_as_systemexit(self) -> None:
        err = CommandFailedError(["python", "-m", "teatree", "followup", "sync"], 1, "", "boom")
        with (
            patch.object(overlay, "_overlay_project_env", return_value=None),
            patch.object(overlay, "run_streamed", side_effect=err),
            pytest.raises(SystemExit) as exc,
        ):
            overlay.managepy_core("followup", "sync")
        assert exc.value.code == 1

    def test_success_does_not_raise(self) -> None:
        with patch.object(overlay, "run_streamed", return_value=0):
            overlay.managepy(None, "queue", "status")  # no raise on clean exit
