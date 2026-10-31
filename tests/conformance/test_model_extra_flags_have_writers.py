"""Every ``Ticket.extra`` / ``Worktree.extra`` key the runtime tree reads is also written somewhere in it.

``more_prs_coming`` was read by ``should_close_ticket`` and written by nothing, so a pull request
that delivered a first step still defaulted to a closing keyword (#5670). A key read and never
written is a flag nobody can raise: the read decides a branch the code can never take.

A writer is any dict display key or subscript store the tree contains (see
``tests.conformance._extra_walk.writer_keys``): a write reaches an ``extra`` through helpers and
returned dicts no receiver rule can follow, so this errs toward finding one. A key set by hand or by
a skill is listed in ``_SET_BY_HAND`` with who sets it; the list can only shrink.
"""

import ast

from tests.conformance._extra_walk import read_keys, string_constants, writer_keys
from tests.conformance._src_tree import REPO_ROOT, src_modules

#: Keys read in src that are set outside it, and by whom. An entry that gains a writer is stale and fails.
_SET_BY_HAND = {
    "claude_only": "an operator pins a ticket to the Claude harness by hand (docs/codex-app-server.md)",
    "notion_url": "the Notion skill records the tracked page through the ticket-sync fields",
    "reaper_pinned": "an operator pins a worktree against the idle-stack reaper by hand",
}


def _runtime_trees() -> list[tuple[str, ast.Module]]:
    return [
        (str(path.relative_to(REPO_ROOT)), tree)
        for path, tree in src_modules()
        if "migrations" not in path.relative_to(REPO_ROOT).parts
    ]


def _read_without_writer(trees: list[tuple[str, ast.Module]]) -> dict[str, list[str]]:
    constants: dict[str, str] = {}
    for _, tree in trees:
        constants |= string_constants(tree)
    written = {key for _, tree in trees for _, key in writer_keys(tree, constants)}
    unwritten: dict[str, list[str]] = {}
    for rel, tree in trees:
        for lineno, key in read_keys(tree):
            if key not in written:
                unwritten.setdefault(key, []).append(f"{rel}:{lineno}")
    return unwritten


def test_every_extra_key_read_in_src_has_a_writer_or_a_named_setter() -> None:
    unexplained = {
        key: sites for key, sites in _read_without_writer(_runtime_trees()).items() if key not in _SET_BY_HAND
    }

    assert not unexplained, "extra key(s) read but never written — a flag nobody can raise:\n" + "\n".join(
        f"  {key}: {', '.join(sites)}" for key, sites in sorted(unexplained.items())
    )


def test_a_key_set_by_hand_is_still_read_without_a_writer() -> None:
    stale = set(_SET_BY_HAND) - set(_read_without_writer(_runtime_trees()))

    assert not stale, f"{sorted(stale)} now have a writer or no reader — drop them from _SET_BY_HAND"


def test_the_walk_flags_a_read_only_key_and_clears_a_written_one() -> None:
    """The control: a planted flag nothing writes turns the check red, and a written one does not."""
    planted = ast.parse(
        "def f(ticket):\n"
        "    ticket.extra.get('zz_read_only_flag')\n"
        "    ticket.extra.get('zz_written_flag')\n"
        "    ticket.merge_extra(set_keys={'zz_written_flag': True})\n"
    )

    assert _read_without_writer([("planted.py", planted)]) == {"zz_read_only_flag": ["planted.py:2"]}
