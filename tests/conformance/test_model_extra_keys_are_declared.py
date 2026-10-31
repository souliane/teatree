"""Every key written into — or read out of — a model ``extra`` is declared on its ``TypedDict``.

``Ticket.extra`` / ``Worktree.extra`` are JSONFields, so an undeclared key is
accepted at the write and dropped silently by the next transition that rewrites
the field through ``validated_*_extra`` — no error, no log, just a value that is
there until it is not. #4152 shipped a revival cap keyed on such a key: the
counter was written on every revival, stripped by the ``test()`` transition on
the way back round the ladder, and so read ``0`` forever. The cap never fired.

Every write shape is walked, because covering only some is a guard that reads as
coverage. #2663 measured that: with only the subscript and ``merge_extra``
literal shapes walked, ``slack_answer`` sat undeclared on 50 live tickets —
written through a constant-named key and a ``get_or_create`` defaults dict,
neither of which the walk could see, and read back at five sites that could only
ever return what the last transition had not yet stripped.

Reads are walked for that last reason: reading a key nothing declares is dead by
construction, so it is a defect wherever the write is. They need a tighter
receiver rule than writes — ``Directive``, ``OutboundClaim`` and
``OuterLoopExperiment`` carry their own ``extra`` JSON that no TypedDict
constrains, so only a receiver provably naming a Ticket/Worktree one counts.

Whole-runtime-tree by construction: the write and the declaration sit in
different modules, so no diff-scoped lane sees both ends. Historical migrations
may read keys that the current model contract intentionally removed.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

from teatree.core.models.types import TicketExtra, WorktreeExtra
from tests.conformance._extra_walk import read_keys, written_keys
from tests.conformance._src_tree import REPO_ROOT, src_modules

# Unioned, not per-model: the AST knows the key, never which model the receiver is.
DECLARED_KEYS = frozenset(TicketExtra.__annotations__) | frozenset(WorktreeExtra.__annotations__)


def _undeclared_writes(path: Path, tree: ast.Module) -> list[str]:
    """Every undeclared model-``extra`` key write in *tree*, as reportable lines."""
    rel = path.relative_to(REPO_ROOT)
    return [
        f"{rel}:{lineno} writes undeclared key '{key}'"
        for lineno, key in sorted(found for found in written_keys(tree) if found[1] not in DECLARED_KEYS)
    ]


def _undeclared_read_lines(path: Path, tree: ast.Module) -> list[str]:
    """Every undeclared model-``extra`` key read in *tree*, as reportable lines."""
    rel = path.relative_to(REPO_ROOT)
    return [
        f"{rel}:{lineno} reads undeclared key '{key}'"
        for lineno, key in sorted(found for found in read_keys(tree) if found[1] not in DECLARED_KEYS)
    ]


def test_every_model_extra_key_written_in_src_is_declared() -> None:
    findings = [finding for path, tree in _runtime_modules() for finding in _undeclared_writes(path, tree)]

    assert not findings, (
        "Undeclared model ``extra`` key(s) — validated_*_extra will drop these on the next transition:\n"
        + "\n".join(findings)
    )


def test_every_model_extra_key_read_in_src_is_declared() -> None:
    findings = [finding for path, tree in _runtime_modules() for finding in _undeclared_read_lines(path, tree)]

    assert not findings, (
        "Undeclared model ``extra`` key(s) read — no write of these survives a transition, so the read is dead:\n"
        + "\n".join(findings)
    )


def _runtime_modules() -> Iterator[tuple[Path, ast.Module]]:
    """Historical data conversions may read keys removed from the current model contract."""
    return ((path, tree) for path, tree in src_modules() if "migrations" not in path.relative_to(REPO_ROOT).parts)


def test_the_walk_detects_every_write_shape() -> None:
    """The control: a walk blind to any one shape passes the tree above for the wrong reason."""
    planted = ast.parse(
        "_KEY = 'zz_undeclared_constant'\n"
        "def f(ticket):\n"
        "    extra = ticket.extra or {}\n"
        "    extra['zz_undeclared_subscript'] = 1\n"
        "    extra[_KEY] = 2\n"
        "    ticket.merge_extra(set_keys={'zz_undeclared_merge': 3})\n"
        "    ticket.merge_extra(merge_into_dicts={_KEY: 4})\n"
        "    ticket.merge_extra(set_keys=cast('TicketExtra', {'zz_undeclared_cast': 5}))\n"
        "    Ticket.objects.get_or_create(url=1, defaults={'extra': {'zz_undeclared_defaults': 6}})\n"
        "    Worktree(extra={'zz_undeclared_ctor': 7})\n"
        "    ticket.merge_extra(set_keys={'branch': 'declared-so-not-a-finding'})\n"
        "    Ticket.objects.create(extra={'labels': ['declared-so-not-a-finding']})\n"
        "    ticket.merge_extra(pop_keys=['zz_popped_is_not_a_write'])\n"
    )

    assert _undeclared_writes(REPO_ROOT / "planted.py", planted) == [
        "planted.py:4 writes undeclared key 'zz_undeclared_subscript'",
        "planted.py:5 writes undeclared key 'zz_undeclared_constant'",
        "planted.py:6 writes undeclared key 'zz_undeclared_merge'",
        "planted.py:7 writes undeclared key 'zz_undeclared_constant'",
        "planted.py:8 writes undeclared key 'zz_undeclared_cast'",
        "planted.py:9 writes undeclared key 'zz_undeclared_defaults'",
        "planted.py:10 writes undeclared key 'zz_undeclared_ctor'",
    ]


def test_the_walk_detects_undeclared_reads_without_flagging_other_models() -> None:
    """The control: reads are found on a typed ``extra``, and only on one."""
    planted = ast.parse(
        "_KEY = 'zz_undeclared_constant'\n"
        "def f(ticket, task, audit):\n"
        "    ticket.extra.get('zz_undeclared_direct')\n"
        "    task.ticket.extra.get(_KEY)\n"
        "    extra = ticket.extra if isinstance(ticket.extra, dict) else {}\n"
        "    extra.get('zz_undeclared_via_name')\n"
        "    ticket.extra.get('branch')\n"
        "    audit.extra.get('zz_other_model_is_not_ours')\n"
    )

    assert _undeclared_read_lines(REPO_ROOT / "planted.py", planted) == [
        "planted.py:3 reads undeclared key 'zz_undeclared_direct'",
        "planted.py:4 reads undeclared key 'zz_undeclared_constant'",
        "planted.py:6 reads undeclared key 'zz_undeclared_via_name'",
    ]
