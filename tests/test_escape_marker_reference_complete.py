"""hooks/CLAUDE.md § "Escape markers & kill-switches" is the canonical catalog.

Doc-invariant guards that keep the catalog from going stale: they derive the live
token set from the circuit breaker's `_SIGNATURE_STRIP_RE` and the live gate names
from the `t3 <overlay> gate` CLI registration, and assert each is documented in
that section. An undocumented new escape token or gate CLI turns CI red.
"""

import re
from pathlib import Path

import hooks.scripts.deny_circuit_breaker as dcb

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOKS_CLAUDE_MD = _REPO_ROOT / "hooks" / "CLAUDE.md"
_GATE_CLI = _REPO_ROOT / "src" / "teatree" / "cli" / "teatree_gate.py"

_SECTION_HEADING = "## Escape markers & kill-switches"


def _canonical_section() -> str:
    """The body of the canonical reference section (heading → next `##` or EOF)."""
    text = _HOOKS_CLAUDE_MD.read_text(encoding="utf-8")
    start = text.index(_SECTION_HEADING)
    rest = text[start + len(_SECTION_HEADING) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _bracket_tokens() -> set[str]:
    """The `[<token>: …]` names the circuit breaker strips — the live token set."""
    alternation = re.search(r"\\\[\(\?:([^)]+)\)", dcb._SIGNATURE_STRIP_RE.pattern)
    assert alternation is not None, "could not parse _SIGNATURE_STRIP_RE bracket alternation"
    return set(alternation.group(1).split("|"))


def _registered_gate_names() -> set[str]:
    """The named `_register_keyed_gate` subcommands attached under `gate`.

    Excludes the bare orchestrator gate (attached directly with no `name=`,
    documented separately as `gate disable`) and the `name="gate"` parent group.
    """
    source = _GATE_CLI.read_text(encoding="utf-8")
    body = source[source.index("def register_gate_commands(") :]
    body = body[: body.index("\ndef ")]
    return set(re.findall(r'_register_keyed_gate\(\s*gate_group,\s*name="([a-z-]+)"', body))


class TestEscapeMarkerReferenceComplete:
    def test_section_exists(self) -> None:
        assert _SECTION_HEADING in _HOOKS_CLAUDE_MD.read_text(encoding="utf-8"), (
            f'hooks/CLAUDE.md must carry the canonical "{_SECTION_HEADING}" reference section.'
        )

    def test_every_active_bracket_token_is_documented(self) -> None:
        section = _canonical_section()
        tokens = _bracket_tokens()
        assert tokens, "expected a non-empty live token set from _SIGNATURE_STRIP_RE"
        missing = sorted(t for t in tokens if f"[{t}:" not in section)
        assert not missing, (
            "The canonical escape-marker reference in hooks/CLAUDE.md is missing "
            f"tokens the circuit breaker actively strips: {missing}. Document each "
            "as `[<token>: <reason>]` in the § 'Escape markers & kill-switches' table."
        )

    def test_every_registered_gate_cli_is_documented(self) -> None:
        section = _canonical_section()
        names = _registered_gate_names()
        assert names, "expected a non-empty gate-name set from register_gate_commands"
        missing = sorted(n for n in names if f"gate {n} disable" not in section)
        assert not missing, (
            "The canonical kill-switch reference in hooks/CLAUDE.md is missing "
            f"self-rescue CLIs for gates registered in teatree_gate.py: {missing}. "
            "Document each as `t3 <overlay> gate <name> disable`."
        )
