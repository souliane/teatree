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

Extraction AND resolution both come from ``teatree.eval.skill_command_validity``
— the one chokepoint the doc-prose lane already uses. This module kept a private
copy of both until #4565, and the copy never received the ``<overlay>``
substitution the doc lane has: its regex required ``t3 [a-z]`` so a
``t3 <overlay> …`` span was never extracted, and its walker broke at the ``<…>``
so a hand-fed one resolved to the bare root. 463 literals written in the
DOCUMENTED overlay-scoped form were unchecked. Sharing the chokepoint is the fix
— a third copy would drift the same way.

Legitimate non-resolving literals are enumerated in ``_ALLOWLIST`` with a
justification each, so the live corpus is clean and a NEW broken literal trips
the gate.
"""

from pathlib import Path

import pytest

from teatree.cli import app, register_overlay_commands
from teatree.cli_reference import command_groups, command_paths
from teatree.eval.skill_command_validity import (
    ALLOWED_NON_RESOLVING,
    citation_resolves,
    iter_backticked_t3_commands,
    resolve_command_path,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = (_REPO_ROOT / "src" / "teatree", _REPO_ROOT / "hooks" / "scripts")

# Legitimate non-resolving literals specific to THIS corpus. The shared
# ``ALLOWED_NON_RESOLVING`` (real commands the in-process introspection cannot
# see) is honoured too, so a command that exists but is unreachable from the
# proxied tree is justified in ONE place rather than drifting across two lists.
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Deliberate drift sample in the cli-reference doctest.
        "t3 loop tickk",
        "t3 questions list|answer|dismiss",
        # A real management command with no overlay proxy leaf — it exists (see
        # overlay.py contract_check) but cannot be resolved against the proxied
        # tree, so it is exempted explicitly.
        "t3 overlay contract-check",
        # Illustrative per-overlay form in a publish-detection parser comment.
        # The real command is the top-level ``t3 review post-comment``; the
        # parser also matches a hypothetical per-overlay variant by substring,
        # which is what the comment shows.
        "t3 teatree review post-comment",
    }
)


def _normalize(raw: str) -> str:
    """Collapse internal whitespace and drop string-concatenation artifacts.

    A literal spanning two adjacent Python string fragments (``"t3 loop "
    "claim-next"``) carries stray ``"`` characters once the backticks span both
    fragments, and an f-string fragment carries a stray ``f`` prefix too. Neither
    ever appears inside a real command name, so dropping both recovers the
    intended invocation. ``f"`` is dropped first — after the bare quotes go, the
    ``f`` is an indistinguishable standalone token.
    """
    return " ".join(raw.replace('f"', " ").replace('"', " ").split())


def _resolves(raw: str, valid: set[str], groups: set[str]) -> bool:
    """True iff *raw* names a live command, or names no concrete command at all.

    Wraps the shared per-citation verdict: ``None`` (a generic mention such as
    ``t3 <overlay> …``, whose path is a placeholder) is not a violation.
    """
    return citation_resolves(raw, valid, groups) is not False


def _iter_literals() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for scan_dir in _SCAN_DIRS:
        for path in sorted(scan_dir.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            found.extend((path, _normalize(raw)) for raw in iter_backticked_t3_commands(text))
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
            if raw in _ALLOWLIST or raw in ALLOWED_NON_RESOLVING:
                continue
            if not _resolves(raw, paths, groups):
                unresolved.append(f"{path.relative_to(_REPO_ROOT)}: `{raw}`")
        assert not unresolved, (
            "t3 command literal(s) in src/ or hooks/ that do not resolve against "
            "the live typer tree (rename/remove, overlay-namespace, or allowlist "
            "with a justification):\n" + "\n".join(unresolved)
        )

    def test_anchor_bug_form_does_not_resolve(self, tree: tuple[set[str], set[str]]) -> None:
        # The bare DJANGO_GROUPS form is exactly the #1982 bug: it must NOT
        # resolve (proving the gate would catch a regression to it).
        paths, groups = tree
        assert resolve_command_path("t3 questions answer 5 hi", paths, groups) is None
        assert not _resolves("t3 questions answer 5 hi", paths, groups)
        assert "t3 questions" not in paths

    def test_fixed_anchor_form_resolves(self, tree: tuple[set[str], set[str]]) -> None:
        paths, groups = tree
        assert _resolves("t3 teatree questions answer 5 hi", paths, groups)


class TestAllowlistIsLive:
    def test_allowlist_entries_are_actually_present_or_example(self, tree: tuple[set[str], set[str]]) -> None:
        # Guard against allowlist rot: every allowlisted literal must still be a
        # genuinely non-resolving form (else it should be removed from the list).
        paths, groups = tree
        stale = [entry for entry in (*_ALLOWLIST, *ALLOWED_NON_RESOLVING) if _resolves(entry, paths, groups)]
        assert not stale, (
            f"allowlist entries now resolve and should be removed (they are no longer legitimate exemptions): {stale}"
        )


class TestOverlayPlaceholderIsResolved:
    """#4565 — the documented ``t3 <overlay> …`` form must be checked, not skipped."""

    def test_bogus_subcommand_behind_the_placeholder_does_not_resolve(self, tree: tuple[set[str], set[str]]) -> None:
        paths, groups = tree
        assert not _resolves("t3 <overlay> ticket bogus-nonexistent", paths, groups)

    def test_real_subcommand_behind_the_placeholder_still_resolves(self, tree: tuple[set[str], set[str]]) -> None:
        # Paired positive: the fix must not degenerate to "reject every placeholder literal".
        paths, groups = tree
        assert _resolves("t3 <overlay> ticket bulk-close", paths, groups)

    def test_corpus_extraction_is_not_blind_to_the_placeholder_form(self) -> None:
        # Layer 1: the extraction regex required `t3 [a-z]`, so `t3 <overlay> …`
        # literals never reached the resolver to begin with.
        assert any(raw.startswith("t3 <overlay> ") for _, raw in _iter_literals())

    def test_f_string_concat_seam_does_not_fake_a_broken_literal(self, tree: tuple[set[str], set[str]]) -> None:
        # `f"…t3 <overlay> worktree " f"release-occupancy {p}…"` — the stray `f`
        # is a concatenation artifact, not a command word.
        paths, groups = tree
        assert _resolves(_normalize('t3 <overlay> worktree " f"release-occupancy {path}'), paths, groups)
