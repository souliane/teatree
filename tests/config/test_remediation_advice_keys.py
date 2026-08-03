# test-path: cross-cutting
"""Runtime advice naming ``config_setting set <key>`` must name a SETTABLE key (#4008).

A gate that refuses an action and prints "fix it with
``t3 <overlay> config_setting set <key> <value>``" is only useful if that command works. The
banned-terms scanner printed exactly that advice for ``banned_terms_required`` — a key read
straight from the ``ConfigSetting`` store but never registered in any config registry — so the CLI
answered ``refusing: 'banned_terms_required' is not a known config setting`` and the operator had
no way to follow the instruction at the moment it mattered.

This is the whole class, not the one key: any in-code string that tells an operator to set a key
is checked against :data:`ALL_KNOWN_CONFIG_SETTINGS`, the same union ``config_setting set``
resolves key-ness through. Scoped to ``src/`` (the advice teatree EMITS at runtime); markdown
docs are out of scope because they legitimately carry templated placeholders
(``require_human_approval_to_*``) that name no single key.
"""

import re
from pathlib import Path

from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS

_SRC = Path(__file__).resolve().parents[2] / "src"

# ``config_setting set <key>`` as it appears in an error/warning string, with or without the
# ``t3 <overlay>`` prefix. A trailing ``<``/``*`` placeholder key is not captured — the character
# class stops at the key's own name.
_ADVICE = re.compile(r"config_setting set ([a-z][a-z0-9_]*)")


def _advised_keys() -> dict[str, list[str]]:
    """Every key named by a ``config_setting set`` instruction in ``src/``, → the files naming it."""
    found: dict[str, list[str]] = {}
    for module in _SRC.rglob("*.py"):
        for key in _ADVICE.findall(module.read_text(encoding="utf-8")):
            found.setdefault(key, []).append(str(module.relative_to(_SRC)))
    return found


def test_enumeration_is_not_vacuous() -> None:
    # Guard the guard: a moved src tree or a broken regex must not let the coverage assertion
    # below pass against an empty enumeration.
    advised = _advised_keys()
    assert _SRC.is_dir()
    assert len(advised) >= 20
    assert "banned_terms_required" in advised


def test_every_advised_key_is_settable() -> None:
    unsettable = {
        key: sorted(set(files)) for key, files in _advised_keys().items() if key not in ALL_KNOWN_CONFIG_SETTINGS
    }
    assert not unsettable, (
        f"advice names keys `config_setting set` refuses: {unsettable}. "
        "Register each in the schema (and so in a config registry), or stop advising it."
    )
