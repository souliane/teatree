"""hooks/CLAUDE.md § "Default-allow" is the canonical gate-refusal policy.

Doc-invariants that keep the policy honest rather than decorative. The leak row is
pinned against :data:`test_gate_never_lockout_contract._NEVER_LOCKOUT_EXEMPT_DENY_HANDLERS`
— the one place the fail-closed set is written down — so the doc cannot claim a gate
stays fail-closed that has since been routed through the fail-open chokepoint, nor
quietly drop one that still is. Every handler the policy names must still be
registered, so an enforcer column cannot go stale the way an escape-marker row can.
"""

import re
from pathlib import Path
from typing import Final

import hooks.scripts.hook_router as router
from tests.test_gate_never_lockout_contract import _NEVER_LOCKOUT_EXEMPT_DENY_HANDLERS

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_HOOKS_CLAUDE_MD: Final[Path] = _REPO_ROOT / "hooks" / "CLAUDE.md"

_SECTION_HEADING: Final[str] = "## Default-allow"

#: The four classes a gate may refuse on its own authority. Each must be named in
#: the policy, so shrinking the carve-out is a visible edit rather than a deletion.
_REFUSED_CLASSES: Final[tuple[str, ...]] = (
    "Secret / credential egress",
    "Public-surface leak",
    "Destroying work",
    "Security / safety risk",
)

#: The row whose enforcers stay fail-CLOSED — pinned against the contract allowlist.
_LEAK_ROW_MARKER: Final[str] = "Public-surface leak"

_HANDLER_IDENT_RE: Final[re.Pattern[str]] = re.compile(r"handle_[a-z0-9_]+")


def _policy_section() -> str:
    """The body of the canonical policy section (heading → next `##` or EOF)."""
    text = _HOOKS_CLAUDE_MD.read_text(encoding="utf-8")
    start = text.index(_SECTION_HEADING)
    rest = text[start + len(_SECTION_HEADING) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _leak_row() -> str:
    """The single table row describing the public-surface-leak class."""
    rows = [line for line in _policy_section().splitlines() if _LEAK_ROW_MARKER in line and line.startswith("|")]
    assert len(rows) == 1, f"expected exactly one {_LEAK_ROW_MARKER!r} table row, found {len(rows)}"
    return rows[0]


def _fail_closed_handlers() -> set[str]:
    """The handlers the never-lockout contract exempts as fail-closed by design."""
    return {name for name, why in _NEVER_LOCKOUT_EXEMPT_DENY_HANDLERS.items() if "fail-closed by design" in why}


def _registered_handler_names() -> set[str]:
    return {handler.__name__ for handlers in router._HANDLERS.values() for handler in handlers}


class TestThePolicyIsPresentAndComplete:
    def test_the_section_exists(self) -> None:
        assert _SECTION_HEADING in _HOOKS_CLAUDE_MD.read_text(encoding="utf-8"), (
            f'hooks/CLAUDE.md must carry the canonical "{_SECTION_HEADING}" policy section.'
        )

    def test_every_refused_class_is_named(self) -> None:
        section = _policy_section()
        missing = [name for name in _REFUSED_CLASSES if name not in section]
        assert missing == [], (
            f"the default-allow carve-out no longer names: {missing}. Narrowing what a "
            "gate may refuse is a deliberate edit, not a silent deletion."
        )


class TestTheLeakRowMatchesTheFailClosedSet:
    """The doc's fail-CLOSED claim and the contract's allowlist are one answer."""

    def test_the_fail_closed_set_is_not_empty(self) -> None:
        # Without this the symmetry below degrades to two empty sets and reads green.
        assert _fail_closed_handlers(), "no fail-closed-by-design handler in the never-lockout allowlist"

    def test_the_leak_row_names_exactly_the_fail_closed_handlers(self) -> None:
        named = set(_HANDLER_IDENT_RE.findall(_leak_row()))
        contract = _fail_closed_handlers()
        assert named == contract, (
            "the public-surface-leak row and the never-lockout fail-closed allowlist "
            f"disagree. only-in-doc={sorted(named - contract)} "
            f"only-in-contract={sorted(contract - named)}"
        )


class TestEveryNamedEnforcerIsLive:
    def test_no_dead_handler_name_in_the_policy(self) -> None:
        named = set(_HANDLER_IDENT_RE.findall(_policy_section()))
        assert named, "the policy names no enforcer — the staleness check below covers nothing"
        dead = sorted(named - _registered_handler_names())
        assert dead == [], f"the default-allow policy names handler(s) the router no longer registers: {dead}"
