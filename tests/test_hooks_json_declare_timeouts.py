"""Every hook registration in hooks.json declares a timeout.

A hook registered without ``timeout`` is unbounded: the harness waits on it for
as long as it takes, with the session blocked behind it.

This is not hypothetical. ``bootstrap-cli.sh`` was registered on SessionStart
with no timeout and called ``t3 doctor check`` synchronously. ``t3`` is
containerized and ``doctor check`` live-probes every enabled MCP connector, so
on a real session that made SessionStart take **13 minutes** before the user
could type a first prompt -- and the call sent its output to /dev/null, so the
whole cost bought a result nobody saw. It was the only one of 18 registrations
missing a timeout; the other 17 were already capped.

The script no longer calls doctor, but a bound belongs on the registration too:
the next hook added without one would reintroduce exactly this, and an unbounded
hook fails in the most expensive place -- session start, before any work.
"""

import json
from pathlib import Path

_HOOKS_JSON = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"

# A hook on the session-start path blocks the user's first prompt, so it gets a
# tighter ceiling than the general one.
_MAX_TIMEOUT_S = 60
_MAX_SESSION_START_TIMEOUT_S = 30  # the bound the other SessionStart hooks already use


def _registrations() -> list[tuple[str, dict]]:
    config = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    return [
        (event, hook)
        for event, matchers in config["hooks"].items()
        for matcher in matchers
        for hook in matcher.get("hooks", [])
    ]


def test_every_hook_registration_declares_a_timeout() -> None:
    undeclared = [(event, hook.get("command", "")) for event, hook in _registrations() if "timeout" not in hook]
    assert not undeclared, (
        "these hook registrations declare no timeout and are therefore unbounded; "
        "an unbounded hook blocks the session for as long as its command runs: " + repr(undeclared)
    )


def test_no_hook_timeout_exceeds_the_ceiling() -> None:
    excessive = [
        (event, hook.get("command", ""), hook["timeout"])
        for event, hook in _registrations()
        if hook.get("timeout", 0) > _MAX_TIMEOUT_S
    ]
    assert not excessive, f"hook timeouts above {_MAX_TIMEOUT_S}s stall the session: {excessive!r}"


def test_session_start_hooks_are_bounded_tightly() -> None:
    slow = [
        (hook.get("command", ""), hook.get("timeout"))
        for event, hook in _registrations()
        if event == "SessionStart" and hook.get("timeout", 0) > _MAX_SESSION_START_TIMEOUT_S
    ]
    assert not slow, (
        f"SessionStart hooks run before the user's first prompt and must stay under "
        f"{_MAX_SESSION_START_TIMEOUT_S}s; slow work belongs detached: {slow!r}"
    )
