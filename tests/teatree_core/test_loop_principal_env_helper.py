"""Tests for ``tests/_loop_principal_env.py`` — the leak-free loop-principal pin (#3810).

``TestPinLeavesNoResidue`` is the regression: it reproduces the sequence that
reddened CI shards 2 and 3 — a module that binds an identity resolver by value
being imported for the FIRST time while a test pins the principal. Under a
``mock.patch`` pin that import stamps the ``MagicMock`` into the importer
permanently, so every later ``t3 <overlay> handover whoami`` in the same worker reports
the pinning test's session id. Under the env pin nothing is replaced, so the
importer is clean the moment the block exits.
"""

import importlib
import os
import sys

from teatree.core.session_identity import (
    RUNNER_PID_ENV,
    RUNNER_SESSION_ENV,
    current_session_id,
    current_session_pid,
    loop_principal,
)
from teatree.utils.env import patched_environ
from tests._loop_principal_env import SESSION_PID_ENV, pinned_loop_principal

_HANDOVER = "teatree.core.management.commands.handover"


class TestPinsTheResolvedPrincipal:
    def test_session_id_resolves_to_the_pinned_value(self) -> None:
        with pinned_loop_principal("sess-pinned"):
            assert current_session_id() == "sess-pinned"
            assert loop_principal()[0] == "sess-pinned"

    def test_pid_resolves_to_the_pinned_value(self) -> None:
        with pinned_loop_principal("sess-pinned", pid=4242):
            assert current_session_pid() == 4242
            assert loop_principal() == ("sess-pinned", 4242)

    def test_default_pins_the_anonymous_principal(self) -> None:
        with pinned_loop_principal():
            assert current_session_id() == ""
            assert loop_principal() == ("", None)


class TestPinOverridesTheAmbientEnvironment:
    """A dev box runs these tests from inside a live session, CI does not."""

    def test_an_ambient_session_id_does_not_win(self) -> None:
        with patched_environ({"CLAUDE_CODE_SESSION_ID": "ambient"}), pinned_loop_principal("sess-pinned"):
            assert current_session_id() == "sess-pinned"

    def test_an_ambient_runner_principal_does_not_win(self) -> None:
        """``loop_principal`` consults the runner pair FIRST — a tick subprocess exports it."""
        ambient = {RUNNER_SESSION_ENV: "loop-runner", RUNNER_PID_ENV: str(os.getpid())}
        with patched_environ(ambient), pinned_loop_principal("sess-pinned"):
            assert loop_principal() == ("sess-pinned", None)


class TestPinLeavesNoResidue:
    def test_a_first_import_under_the_pin_is_not_stamped(self) -> None:
        """The #3810 CI red, through the door the delegating entry point leaves open.

        ``handover`` binds ``current_session_id`` off the loop entry point with a
        module-level ``from ... import``, and Django imports commands lazily. Under
        ``mock.patch`` of that attribute this assertion is RED — the command keeps
        answering ``sess-pinned`` long after the block exits.
        """
        original = importlib.import_module(_HANDOVER)
        try:
            del sys.modules[_HANDOVER]
            with pinned_loop_principal("sess-pinned"):
                command = importlib.import_module(_HANDOVER)
                assert command.current_session_id() == "sess-pinned"
            assert command.current_session_id() != "sess-pinned"
        finally:
            sys.modules[_HANDOVER] = original

    def test_the_environment_is_restored(self) -> None:
        before = dict(os.environ)
        with pinned_loop_principal("sess-pinned", pid=4242):
            assert os.environ[SESSION_PID_ENV] == "4242"
        assert dict(os.environ) == before
