"""SessionStart advisory naming an under-bounded hook in the operator's OWN settings.

``tests/test_hooks_json_declare_timeouts.py`` pins that every SessionStart registration
in this repo's ``hooks/hooks.json`` declares a timeout at or under the ceiling. It can
reach nothing else, and the machine-local agent settings register hooks on the same
chain — which is where an unbounded ``t3 doctor check`` came back after this repo's own
copy had removed it.

No other venue can see that file. ``t3`` on a dockerised install is a launcher that
execs into the worker, and the container generates its own ``~/.claude/settings.json``
from a baked template rather than mounting the host's — so ``t3 doctor check`` reads a
different file than the one a session actually starts from. The SessionStart hook runs
on the host, which leaves it the only reader, and it pays one JSON parse.
"""

from pathlib import Path

from hooks.scripts.managed_repo import teatree_src_on_path as _teatree_src_on_path


def session_start_hook_budget_advisory() -> str | None:
    """The advisory for the operator's settings, or ``None`` when there is nothing to say."""
    try:
        with _teatree_src_on_path():
            from teatree.core.session_start_hook_budget import advisory_text  # noqa: PLC0415 — cold-hook import

            return advisory_text(Path.home() / ".claude" / "settings.json") or None
    except Exception:  # noqa: BLE001 — never block SessionStart on a settings read hiccup
        return None
