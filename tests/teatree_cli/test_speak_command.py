"""``t3 speak`` management command — the detached Stop-hook worker (#1791).

Bootstraps Django, then calls :func:`teatree.core.speak.speak` with
``block=True`` (synchronous so the short-lived subprocess delivers before
exiting). ``--overlay`` sets ``T3_OVERLAY_NAME`` for the call. Only the
``speak`` seam is mocked; the command's argument plumbing + overlay-env
context manager run for real.
"""

import io
import os
from unittest.mock import patch

from django.core.management import call_command


class TestSpeakCommand:
    def test_calls_speak_blocking_with_text(self) -> None:
        with patch("teatree.core.speak.speak") as speak:
            call_command("speak", "hello there")
        speak.assert_called_once_with("hello there", block=True)

    def test_reads_text_from_stdin_on_dash(self) -> None:
        with (
            patch("teatree.core.speak.speak") as speak,
            patch("sys.stdin", io.StringIO("piped body")),
        ):
            call_command("speak", "-")
        speak.assert_called_once_with("piped body", block=True)

    def test_overlay_sets_env_for_the_call_and_restores(self) -> None:
        seen: dict[str, str] = {}

        def capture(_text: str, *, block: bool) -> None:
            _ = block
            seen["overlay"] = os.environ.get("T3_OVERLAY_NAME", "")

        os.environ.pop("T3_OVERLAY_NAME", None)
        with patch("teatree.core.speak.speak", side_effect=capture):
            call_command("speak", "hi", overlay="teatree")
        assert seen["overlay"] == "teatree"
        assert "T3_OVERLAY_NAME" not in os.environ

    def test_overlay_restores_prior_env_value(self, monkeypatch: object) -> None:
        seen: dict[str, str] = {}

        def capture(_text: str, *, block: bool) -> None:
            _ = block
            seen["overlay"] = os.environ.get("T3_OVERLAY_NAME", "")

        os.environ["T3_OVERLAY_NAME"] = "original"
        try:
            with patch("teatree.core.speak.speak", side_effect=capture):
                call_command("speak", "hi", overlay="teatree")
            assert seen["overlay"] == "teatree"
            assert os.environ["T3_OVERLAY_NAME"] == "original"
        finally:
            os.environ.pop("T3_OVERLAY_NAME", None)

    def test_no_overlay_leaves_env_untouched(self) -> None:
        os.environ.pop("T3_OVERLAY_NAME", None)
        with patch("teatree.core.speak.speak") as speak:
            call_command("speak", "hi")
        speak.assert_called_once_with("hi", block=True)
        assert "T3_OVERLAY_NAME" not in os.environ
