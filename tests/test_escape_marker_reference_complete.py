"""hooks/CLAUDE.md § "Escape markers & kill-switches" is the canonical catalog.

Doc-invariant guards that keep the catalog from going stale: they derive the live
token set from the GATES THAT IMPLEMENT the markers and the live gate names
from the `t3 <overlay> gate` CLI registration, and assert each is documented in
that section. An undocumented new escape token or gate CLI turns CI red.

The implementation is the source of truth because the two weaker candidates each
answer a subset. Deriving from the circuit breaker's `_SIGNATURE_STRIP_RE` — what
this lane used to do — asks the catalog to cover the 8 markers that regex happened
to name while 24 were implemented, so four went undocumented and the lane stayed
green. Deriving from the catalog asks nothing of the code at all.

These check MENTION ONLY — that the token/gate NAME appears. A row whose stated
gate or scan surface has since gone stale is invisible here (#4216: an
`[admission-ok:]` row kept naming a retired arm through three passes). The row's
CONTENT is pegged by `tests/quality/anchor_prose_pegs.toml`.
"""

import re
from pathlib import Path

import hooks.scripts.deny_circuit_breaker as dcb

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOKS_CLAUDE_MD = _REPO_ROOT / "hooks" / "CLAUDE.md"
_GATE_CLI = _REPO_ROOT / "src" / "teatree" / "cli" / "teatree_gate.py"

_SECTION_HEADING = "## Escape markers & kill-switches"

#: Where a gate lives. A marker is implemented as a private compiled pattern in the
#: module that scans for it — there is no shared seam, so the pattern IS the seam.
_GATE_ROOTS = ("hooks/scripts", "src/teatree/hooks", "src/teatree/core/gates", "src/teatree/cli/review")
_IMPLEMENTED_MARKER_RE = re.compile(r"\\\[([a-z][a-z0-9-]*):")


def _canonical_section() -> str:
    """The body of the canonical reference section (heading → next `##` or EOF)."""
    text = _HOOKS_CLAUDE_MD.read_text(encoding="utf-8")
    start = text.index(_SECTION_HEADING)
    rest = text[start + len(_SECTION_HEADING) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _implemented_markers(roots: tuple[str, ...] = _GATE_ROOTS) -> set[str]:
    """Every `[<token>: …]` a gate module compiles a pattern for — the live token set."""
    found: set[str] = set()
    for name in roots:
        root = _REPO_ROOT / name
        assert root.is_dir(), f"gate root {root} is missing — an empty walk would report a clean catalog"
        for path in root.rglob("*.py"):
            found |= set(_IMPLEMENTED_MARKER_RE.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return found


def _stripped_tokens() -> set[str]:
    """The `[<token>: …]` names the circuit breaker folds out of a call signature."""
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

    def test_every_implemented_marker_is_documented(self) -> None:
        section = _canonical_section()
        markers = _implemented_markers()
        assert len(markers) > 15, "a shrunken walk would make the assertion below trivially true"
        missing = sorted(t for t in markers if f"[{t}:" not in section)
        assert not missing, (
            "The canonical escape-marker reference in hooks/CLAUDE.md is missing "
            f"tokens a gate implements: {missing}. Document each "
            "as `[<token>: <reason>]` in the § 'Escape markers & kill-switches' table."
        )

    def test_a_marker_no_gate_implements_is_not_counted(self) -> None:
        """The control. Without it, a walk that reads nothing reports the same clean answer."""
        planted = "a-marker-no-gate-anywhere-compiles"

        assert planted not in _implemented_markers()

    def test_every_implemented_marker_is_stripped_from_a_deny_signature(self) -> None:
        # The breaker counts identical denials, so a token it does not fold out splits one
        # retry loop into one fingerprint per reason text and the threshold is never reached.
        unstripped = sorted(_implemented_markers() - _stripped_tokens())

        assert not unstripped, (
            f"escape markers absent from deny_circuit_breaker._SIGNATURE_STRIP_RE: {unstripped} — "
            "add each to its bracket alternation so the same call with and without the token "
            "maps to one fingerprint"
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
