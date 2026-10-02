"""The operator's OWN SessionStart registrations, checked for a bound the harness enforces.

``tests/test_hooks_json_declare_timeouts.py`` pins that every registration in this
repo's ``hooks/hooks.json`` declares a ``timeout``. It cannot reach the machine-local
agent settings, which register hooks on the same chain — and that is precisely where an
unbounded ``t3 doctor check`` reappeared after this repo's own copy had removed it,
costing a measured 18.94s per-hook against a 15s budget and 14 minutes under load.

So the same two rules are applied to the file that test cannot see, from the only venue
that can read it: the SessionStart hook itself, on the host, for one JSON parse.
"""

import json
from collections.abc import Iterator
from pathlib import Path

SESSION_START_TIMEOUT_CEILING_S = 30


def _registrations(settings: object) -> Iterator[dict]:
    if not isinstance(settings, dict):
        return
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for matcher in hooks.get("SessionStart") or []:
        if not isinstance(matcher, dict):
            continue
        for hook in matcher.get("hooks") or []:
            if isinstance(hook, dict):
                yield hook


def _offences(settings: object) -> list[str]:
    offences = []
    for hook in _registrations(settings):
        command = str(hook.get("command") or "").strip() or "<no command>"
        timeout = hook.get("timeout")
        if not isinstance(timeout, int | float):
            # A non-numeric timeout (e.g. a JSON string "45") is unenforceable by the
            # harness that reads it — treat it exactly like a missing one, never as clean.
            offences.append(f"  UNBOUNDED (no `timeout`): {command}")
        elif timeout > SESSION_START_TIMEOUT_CEILING_S:
            offences.append(f"  {timeout}s exceeds the {SESSION_START_TIMEOUT_CEILING_S}s ceiling: {command}")
    return offences


def advisory_text(settings_path: Path) -> str:
    """The advisory naming every under-bounded SessionStart hook in *settings_path*.

    Empty when the file is clean, absent, or unparsable — a settings file the harness
    itself could not load registered none of these hooks in the first place.
    """
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    offences = _offences(settings)
    if not offences:
        return ""
    return (
        "TEATREE — a SessionStart hook in your own settings is under-bounded.\n\n"
        f"{settings_path} registers these on the session-start chain:\n\n"
        + "\n".join(offences)
        + "\n\nAn unbounded hook blocks the session for as long as its command runs, before a "
        "first prompt can be typed. Give each one a `timeout` (seconds) at or under "
        f"{SESSION_START_TIMEOUT_CEILING_S}, or drop the registration. This repo's own "
        "registrations are all capped, and the test that pins that cannot see this file."
    )
