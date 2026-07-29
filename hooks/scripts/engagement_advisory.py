"""The SessionStart "teatree is not active" advisory, and WHY it is not active (#3499).

A not-engaged session gets one line telling the operator how to start teatree. That
line used to be unconditional: *"run `t3 <overlay> config_setting set autoload true`
to start it automatically"*. When the cold reader could not reach the config DB at
all, `autoload` resolved to its fail-closed default and the operator was told to set
a flag they had **already set** — the advice actively pointed away from the real
fault, and a broken install was indistinguishable from an opt-out.

So the advisory branches on :func:`autoload_resolution`, which reports not just
whether autoload is on but how that answer was reached. An UNREADABLE store gets a
different line naming the breakage and pointing at ``t3 doctor check``; every other
case keeps the original how-to-start text.

Fail-closed is unchanged in both cases: an unreadable store still does NOT
auto-engage. This module only changes what the operator is TOLD, never whether
teatree starts.

Cold-import safe: stdlib-only at module top. ``teatree_settings`` is imported lazily
inside the functions, both because the leaf must stay import-light for the fast-hook
budget and because ``teatree_settings`` itself delegates back here — the lazy import
is what keeps that mutual reference from being a load-time cycle.
"""

import os
import sys

# Alias both identities so a bare ``from engagement_advisory import ...`` (the live
# hook, whose dir is on sys.path) and ``hooks.scripts.engagement_advisory`` (a
# subprocess/test import) resolve the SAME module object — the pattern every sibling
# leaf uses.
sys.modules.setdefault("engagement_advisory", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.engagement_advisory", sys.modules[__name__])

# :func:`autoload_resolution` second-element vocabulary — HOW the answer was reached.
AUTOLOAD_FROM_ENV = "env"
AUTOLOAD_FROM_DB = "db"
AUTOLOAD_FROM_DEFAULT = "default"
AUTOLOAD_UNREADABLE = "unreadable"

TEATREE_NOT_ACTIVE_ADVISORY = (
    "teatree is installed but not active in this session — run /teatree to start it "
    "(or run `t3 <overlay> config_setting set autoload true` to start it automatically)."
)

# Deliberately does NOT suggest setting `autoload`: the store cannot be read, so the
# flag's value is unknown and may already be true. Naming the blast radius matters —
# every cold-hook gate kill-switch resolves through the same reader, so they are all
# silently running on their built-in defaults, not on what the operator configured.
TEATREE_SETTINGS_UNREADABLE_ADVISORY = (
    "teatree is installed but could NOT read its settings store, so it did not "
    "auto-start and every cold-hook gate is running on its built-in default rather "
    "than your configuration. This is a broken install, not an opt-out — run "
    "`t3 doctor check` to diagnose (typically the hook's interpreter cannot import "
    "teatree). /teatree still starts this session manually."
)


def autoload_resolution() -> tuple[bool, str]:
    """Whether teatree auto-engages a fresh session (#256), and HOW that was decided.

    Returns ``(enabled, source)`` where ``source`` is one of :data:`AUTOLOAD_FROM_ENV`,
    :data:`AUTOLOAD_FROM_DB`, :data:`AUTOLOAD_FROM_DEFAULT`, or
    :data:`AUTOLOAD_UNREADABLE`.

    Resolution order is unchanged from the flag's original behaviour: ``T3_AUTOLOAD``
    truthy first (it needs no teatree import, so it is the escape hatch when the store
    IS broken), else the DB-home ``autoload`` row, else fail CLOSED to off. The only
    addition is that a store the reader could not reach is reported as
    :data:`AUTOLOAD_UNREADABLE` instead of being flattened into "off", so the caller
    can tell a broken install from an operator who never opted in.

    ``teatree_settings.autoload_enabled`` deliberately does NOT delegate here: it must
    stay reachable under the BARE ``teatree_settings`` identity with only the scripts
    dir on ``sys.path`` (the live hook's own path setup), which importing this sibling
    would jeopardise. The two therefore encode the same order independently, and
    ``TestAutoloadResolutionMatchesEnabled`` pins them equal across every case so the
    duplication cannot drift.
    """
    from hooks.scripts.teatree_settings import (  # noqa: PLC0415 — deferred: cold-hook import
        _AUTOLOAD_TRUTHY,
        COLD_READ_UNREADABLE,
        read_cold_setting_status,
    )

    env = os.environ.get("T3_AUTOLOAD", "").strip().lower()
    if env:
        return env in _AUTOLOAD_TRUTHY, AUTOLOAD_FROM_ENV
    value, status = read_cold_setting_status("autoload")
    if status == COLD_READ_UNREADABLE:
        return False, AUTOLOAD_UNREADABLE
    if isinstance(value, bool):
        return value, AUTOLOAD_FROM_DB
    return False, AUTOLOAD_FROM_DEFAULT


def session_start_advisory() -> str:
    """The one-line how-to-start advisory for a fresh, not-engaged session.

    Names the read failure when the settings store is unreachable; otherwise gives the
    original how-to-start line. Crash-proof: any failure resolving the reason degrades
    to the original text, so the advisory can never be the thing that breaks
    SessionStart.
    """
    try:
        _, source = autoload_resolution()
    except Exception:  # noqa: BLE001 — crash-proof hook: never break SessionStart over advisory wording
        return TEATREE_NOT_ACTIVE_ADVISORY
    if source == AUTOLOAD_UNREADABLE:
        return TEATREE_SETTINGS_UNREADABLE_ADVISORY
    return TEATREE_NOT_ACTIVE_ADVISORY
