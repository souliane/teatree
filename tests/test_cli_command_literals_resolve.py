"""Every ``t3 …`` command literal in src/ + hooks/ resolves against the live tree (#1982).

The skill-prose validator (``test_skill_t3_invocations.py``) gates ``t3 …``
literals cited in SKILL.md. This is its companion for **error-message and hook
strings**: a backticked ``t3 GROUP SUB`` literal embedded in a user-facing
string under ``src/teatree/`` or ``hooks/scripts/`` must resolve against the
introspected Typer + overlay command tree. A renamed/removed/overlay-mismatched
command cited in a message that the user is told to run becomes a commit-time
failure for the whole class.

Resolution is overlay-aware: a DJANGO_GROUPS command is namespaced under the
overlay (``t3 teatree questions answer``), never a bare ``t3 questions answer``
(which returns "No such command 'questions'"). The anchor bug this gate pins:
``core/notify.py`` told the user to run the bare form in the away-mode Slack DM.

Legitimate non-resolving literals — example-overlay placeholders, doctest drift
samples, and command shapes owned by an in-flight sibling PR — are enumerated in
``_ALLOWLIST`` with a justification each, so the live corpus is clean and a NEW
broken literal trips the gate.
"""

import re
from pathlib import Path

import pytest

from teatree.cli import app, register_overlay_commands
from teatree.cli_reference import command_groups, command_paths

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = (_REPO_ROOT / "src" / "teatree", _REPO_ROOT / "hooks" / "scripts")

_T3_IN_BACKTICKS = re.compile(r"`(t3 [a-z][^`]*)`")
# A token that ends the command path: an option/flag, a shell placeholder, an
# ASCII or unicode ellipsis, a redirect/pipe, a quote or ``#`` (string-concat /
# comment artifacts), a brace/bracket/angle placeholder, a ``a|b`` enumeration,
# or an arg value.
_PLACEHOLDER = re.compile(r"^(\.\.\.|…|<.*>|\$.*|--.*|-[A-Za-z]|\{.*\}|.*\|.*|>.*|\".*|'.*|\[.*|#.*)$")

# Overlay names that appear in docs/examples but are not real registered
# overlays — a literal whose second token is one of these is a templated
# example, not a drifted command.
_EXAMPLE_OVERLAYS = frozenset({"acme", "example", "myoverlay"})

# Files owned by an in-flight sibling PR (#2000, open) — not touched here to
# avoid a merge collision. Their bare ``t3 workspace …`` literals are real drift
# the gate picks up after that PR merges; grandfathered (not exempted) until then.
_CARVED_OUT_FILES = frozenset(
    {
        "src/teatree/core/management/commands/_workspace_cleanup.py",
    }
)

# Legitimate non-resolving literals. Each is either a templated/example form, a
# deliberate drift sample, or a command shape an in-flight sibling PR owns
# (carved out of this PR — the resolver picks them up after that PR lands).
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Deliberate drift sample in the cli-reference doctest.
        "t3 loop tickk",
        # `claim/owner/release` is a slash-joined enumeration, not a path.
        "t3 loop claim/owner/release",
        "t3 availability away|present|auto",
        "t3 questions list|answer|dismiss",
        # Real commands not surfaced by the in-process introspection used here
        # (DJANGO_GROUPS management subcommands without an overlay proxy leaf, or
        # commands on a sibling surface). They exist — see overlay.py
        # contract_check — but cannot be resolved against the proxied tree, so
        # they are exempted explicitly.
        "t3 overlay contract-check",
        # Illustrative per-overlay form in a publish-detection parser comment.
        # The real command is the top-level ``t3 review post-comment``; the
        # parser also matches a hypothetical per-overlay ``t3 <overlay> review
        # post-comment`` variant by substring, which is what the comment shows.
        "t3 teatree review post-comment",
    }
)


def _normalize(raw: str) -> str:
    """Collapse internal whitespace and drop string-concatenation quote artifacts.

    A literal spanning two adjacent Python string fragments (``"t3 loop "
    "claim-next"``) carries stray ``"`` characters once the backticks span both
    fragments. A quote never appears inside a real command name, so dropping it
    recovers the intended invocation.
    """
    return " ".join(raw.replace('"', " ").split())


def _resolvable_path(raw: str, valid: set[str], groups: set[str]) -> str | None:
    toks = raw.split()
    if not toks or toks[0] != "t3":
        return None
    matched = "t3"
    for tok in toks[1:]:
        if _PLACEHOLDER.match(tok):
            break
        nxt = f"{matched} {tok}"
        if nxt in valid:
            matched = nxt
            continue
        if matched in groups:
            return None
        break
    return matched if matched in valid else None


def _resolves(raw: str, valid: set[str], groups: set[str]) -> bool:
    """True iff *raw* resolves AS WRITTEN, or is a templated example-overlay form.

    Deliberately does NOT auto-namespace a bare literal up to the overlay form:
    the #1982 bug is exactly a bare ``t3 questions answer`` that the user is told
    to run but that returns "No such command 'questions'". The literal must
    already carry the overlay segment (``t3 teatree questions answer``) — that is
    the canonical, runnable form. A bare DJANGO_GROUPS literal must fail.
    """
    toks = raw.split()
    if len(toks) >= 2 and toks[1] in _EXAMPLE_OVERLAYS:
        return True
    return _resolvable_path(raw, valid, groups) is not None


def _iter_literals() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for scan_dir in _SCAN_DIRS:
        for path in sorted(scan_dir.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            found.extend((path, _normalize(m.group(1))) for m in _T3_IN_BACKTICKS.finditer(text))
    return found


@pytest.fixture(scope="module")
def tree() -> tuple[set[str], set[str]]:
    register_overlay_commands(allowlist={"t3-teatree"})
    return command_paths(app), command_groups(app)


class TestCommandLiteralsResolve:
    def test_every_t3_literal_in_src_and_hooks_resolves(self, tree: tuple[set[str], set[str]]) -> None:
        paths, groups = tree
        unresolved: list[str] = []
        for path, raw in _iter_literals():
            rel = str(path.relative_to(_REPO_ROOT))
            if raw in _ALLOWLIST or rel in _CARVED_OUT_FILES:
                continue
            if not _resolves(raw, paths, groups):
                unresolved.append(f"{rel}: `{raw}`")
        assert not unresolved, (
            "t3 command literal(s) in src/ or hooks/ that do not resolve against "
            "the live typer tree (rename/remove, overlay-namespace, or allowlist "
            "with a justification):\n" + "\n".join(unresolved)
        )

    def test_anchor_bug_form_does_not_resolve(self, tree: tuple[set[str], set[str]]) -> None:
        # The bare DJANGO_GROUPS form is exactly the #1982 bug: it must NOT
        # resolve (proving the gate would catch a regression to it).
        paths, groups = tree
        assert _resolvable_path("t3 questions answer 5 hi", paths, groups) is None
        assert "t3 questions" not in paths

    def test_fixed_anchor_form_resolves(self, tree: tuple[set[str], set[str]]) -> None:
        paths, groups = tree
        assert _resolves("t3 teatree questions answer 5 hi", paths, groups)


class TestAllowlistIsLive:
    def test_allowlist_entries_are_actually_present_or_example(self, tree: tuple[set[str], set[str]]) -> None:
        # Guard against allowlist rot: every allowlisted literal must still be a
        # genuinely non-resolving form (else it should be removed from the list).
        paths, groups = tree
        stale = [entry for entry in _ALLOWLIST if _resolves(entry, paths, groups)]
        assert not stale, (
            f"allowlist entries now resolve and should be removed (they are no longer legitimate exemptions): {stale}"
        )
