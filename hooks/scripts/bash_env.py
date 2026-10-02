"""Read loop control vars out of the shell-sourceable teatree env file (``$HOME/.teatree``).

Bare sibling of ``hook_router`` (hooks/CLAUDE.md: hook logic lives in a sibling
module, never in the shrink-only-capped router). The harness spawns hooks as a bare
``python3`` that does NOT source the user's shell profile, so ``export VAR=value``
lines in that file never reach ``os.environ``. :func:`resolve_loop_env` recovers
them — process env first, file second — which is how the unsourced Stop hook still
sees the ``T3_LOOP_DISOWN`` kill switch.

Pure stdlib by construction: no ``teatree`` import (the hook interpreter may lack
it, #810) and no shell invocation, so the parse works on the coldest hook path.
Crash-proof — a missing or unreadable file yields ``""``, never an exception.
"""

import os
import sys
from pathlib import Path

# Alias the bare and ``hooks.scripts.`` identities so the router and any test
# patching a helper here operate on ONE module object.
sys.modules.setdefault("bash_env", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.bash_env", sys.modules[__name__])


def bash_env_file() -> Path:
    """Path to the shell-sourceable teatree env file (``$HOME/.teatree``).

    ``TEATREE_BASH_ENV_FILE`` overrides the location (tests / non-default HOME).
    """
    override = os.environ.get("TEATREE_BASH_ENV_FILE", "").strip()
    if override:
        return Path(override)
    return Path(os.environ.get("HOME", str(Path.home()))) / ".teatree"


def strip_bash_value(rest: str) -> str:
    """Strip surrounding quotes and a trailing ``# comment``."""
    rest = rest.strip()
    quote = rest[0] if rest[:1] in {"'", '"'} else ""
    if quote:
        end = rest.find(quote, 1)
        if end != -1:
            return rest[1:end]
        return rest[1:]
    return rest.split("#", 1)[0].strip()


def read_bash_env_var(name: str) -> str:
    """Last ``export <name>=<value>`` value in :func:`bash_env_file`.

    Tolerant of a leading ``export``/whitespace, spaces around ``=``,
    single/double quotes, and trailing ``# comments``. Last assignment wins,
    mirroring shell sourcing.
    """
    try:
        path = bash_env_file()
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    value = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, rest = line.partition("=")
        if not sep or key.strip() != name:
            continue
        value = strip_bash_value(rest)
    return value


def resolve_loop_env(name: str) -> str:
    """Resolve a loop control var: process env first, bash env file second.

    The process env is authoritative — an explicit value (even empty) there is
    never overridden by the file. The file is consulted only when the var is
    wholly absent from ``os.environ``, recovering the kill-switch the unsourced
    Stop hook would otherwise miss.
    """
    if name in os.environ:
        return os.environ[name]
    return read_bash_env_var(name)
