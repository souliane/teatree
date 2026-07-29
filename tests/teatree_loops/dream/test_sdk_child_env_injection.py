"""The dream SDK turns take their credential child-env as an injected seam (#3512).

Both turn functions used to resolve the credential env themselves, which reaches
``PassPathSelector._configured_paths`` → ``ConfigSetting.objects.get_effective`` —
an uncaught DB read. Django forbids that from a ``SimpleTestCase``, so the parse and
watchdog unit tests failed nondeterministically, and in the threaded watchdog case the
``DatabaseOperationForbidden`` REPLACED the ``TimeoutError`` under assertion, pointing
the reader at the watchdog instead of at the layering violation several frames away.

The control that makes these tests anti-vacuous is the pinned provider: without it
the resolver short-circuits to ``None`` before the DB read, so a green proves nothing.
``test_pinned_provider_control_still_reaches_the_db_without_injection`` asserts the
un-injected call DOES raise, so the injected cases' green is real.
"""

import threading
from pathlib import Path
from unittest.mock import patch

import claude_agent_sdk
import pytest
from django.test import SimpleTestCase
from django.test.testcases import DatabaseOperationForbidden  # ty: ignore[unresolved-import]

from teatree.agents._headless_env import system_child_env
from teatree.loops.dream import sdk_distiller, sdk_eval_synthesizer
from teatree.loops.dream.engine import ConsolidationExtract, WeightedSnippet
from tests.teatree_agents._sdk_fake import FakeHarnessSession, assistant_text

_PINNED = {"T3_AGENT_HARNESS_PROVIDER": "subscription_oauth"}


def _extract() -> ConsolidationExtract:
    return ConsolidationExtract(
        snippets=(WeightedSnippet(path=Path("/feedback_x.md"), kind="memory", weight=9, text="BINDING: x"),),
        truncated=False,
    )


def _no_credentials() -> dict[str, str] | None:
    return None


class ChildEnvInjectionTestCase(SimpleTestCase):
    def test_pinned_provider_control_still_reaches_the_db_without_injection(self) -> None:
        with patch.dict("os.environ", _PINNED), pytest.raises(DatabaseOperationForbidden):
            system_child_env()

    def test_distiller_turn_honours_the_injected_resolver(self) -> None:
        reply = "[]"
        with (
            patch.dict("os.environ", _PINNED),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(claude_agent_sdk, "ClaudeSDKClient", lambda **_: FakeHarnessSession([assistant_text(reply)])),
        ):
            assert sdk_distiller._run_distiller_turn(_extract(), child_env=_no_credentials) == reply

    def test_distill_entry_point_threads_the_resolver_down(self) -> None:
        with (
            patch.dict("os.environ", _PINNED),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(claude_agent_sdk, "ClaudeSDKClient", lambda **_: FakeHarnessSession([assistant_text("[]")])),
        ):
            assert sdk_distiller.sdk_distiller(_extract(), child_env=_no_credentials) == []

    def test_synthesizer_turn_honours_the_injected_resolver(self) -> None:
        with (
            patch.dict("os.environ", _PINNED),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(claude_agent_sdk, "ClaudeSDKClient", lambda **_: FakeHarnessSession([assistant_text("{}")])),
            pytest.raises(ValueError, match="missing required key"),  # a malformed reply, NOT a DB error
        ):
            sdk_eval_synthesizer.sdk_spec_synthesizer({}, "slice", child_env=_no_credentials)

    def test_injected_resolver_survives_the_threaded_watchdog_frame(self) -> None:
        # The nastiest shape: the DB read happened inside the watchdog THREAD, so its
        # exception masqueraded as a watchdog failure. With the resolver injected the
        # thread never touches the DB.
        captured: dict[str, BaseException | None] = {"exc": None}

        def _run() -> None:
            try:
                sdk_distiller._run_distiller_turn(_extract(), child_env=_no_credentials)
                captured["exc"] = None
            except BaseException as exc:  # noqa: BLE001 — record whatever the turn raised
                captured["exc"] = exc

        with (
            patch.dict("os.environ", _PINNED),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.object(claude_agent_sdk, "ClaudeSDKClient", lambda **_: FakeHarnessSession([assistant_text("[]")])),
        ):
            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            thread.join(timeout=30)

        assert captured["exc"] is None, repr(captured["exc"])
