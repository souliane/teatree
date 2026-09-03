"""Shared helpers to PIN ``on_behalf_post_mode`` for a test that depends on it.

The tri-state ``on_behalf_post_mode`` gate (#960) is enforced at the
``_BaseReplier`` chokepoint. Tests that exercise transport *mechanics*
(idempotency, status recording, backend wiring) — not the gate, which
has its own dedicated suites — need IMMEDIATE mode (gate off) so their
assertions still hold.

The pin runs BOTH ways. No tier collapses the mode (#3895), so an UNSET
mode resolves the shipped ``draft_or_ask`` at every tier — gate-ON. A test
about the gate BLOCKING still pins it explicitly (:func:`mode_gate_on_cm`)
rather than leaning on that default, so the case states what it exercises
and survives a later change of shipped value.

Under the #1775 DB-home partition ``on_behalf_post_mode`` is DB-home
(legacy file tier removed). These helpers set the mode through the
``T3_ON_BEHALF_POST_MODE`` env var — the highest-precedence tier, which
wins for a DB-home key and needs no database, so the helper works for
every caller (DB-backed or not) without a ``ConfigSetting`` write.

Three are exported because the direction and the consumers differ:

*   :func:`mode_immediate_cm` — gate OFF as a ``contextlib`` context manager
    (works for both ``unittest.TestCase`` classes and pytest functions that
    prefer ``with mode_immediate_cm(): ...``).
*   :func:`mode_gate_on_cm` — the same shape for the opposite direction, for a
    test that needs the gate to BLOCK.
*   :func:`disable_on_behalf_gate` — gate OFF as a one-shot helper that sets the
    env var for the lifetime of the test using pytest's ``monkeypatch``
    fixture, so an autouse fixture can call it without context-manager
    scoping.

A mode is only HALF the precondition on the review surface, where the post names
its target repo: the verdict reads the mode of the overlay that OWNS that repo,
and a target no overlay owns has no overlay tier of its own — inheriting the
ambient overlay's is the mis-attribution the gate closes, so a pin set for an
overlay never reaches a throwaway ``org/repo``. :data:`OWNED_REPO` supplies the
other half, making the pin a test sets the one the gate actually reads.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest

_MODE_ENV = "T3_ON_BEHALF_POST_MODE"
_MODE_IMMEDIATE_ENV = "immediate"
_MODE_GATE_ON_ENV = "draft_or_ask"

#: A review target the gate can read a mode FOR — owned by ``t3-teatree``, the
#: in-repo overlay ``tests/conftest.py`` pins (its ``get_workspace_repos()``
#: lists ``teatree``).
OWNED_REPO = "souliane/teatree"


@contextmanager
def mode_immediate_cm() -> Iterator[None]:
    """Context manager: ``on_behalf_post_mode`` is IMMEDIATE inside the block."""
    with patch.dict(os.environ, {_MODE_ENV: _MODE_IMMEDIATE_ENV}):
        yield


@contextmanager
def mode_gate_on_cm() -> Iterator[None]:
    """Context manager: the gate BLOCKS colleague-visible posts inside the block."""
    with patch.dict(os.environ, {_MODE_ENV: _MODE_GATE_ON_ENV}):
        yield


def disable_on_behalf_gate(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set ``on_behalf_post_mode`` to IMMEDIATE (DB-home, via env) for this test.

    Designed for autouse fixtures that disable the gate for the lifetime
    of a class/function-scoped test: ``monkeypatch`` reverts the env var at
    teardown so the global gate-on default is restored automatically.
    """
    monkeypatch.setenv(_MODE_ENV, _MODE_IMMEDIATE_ENV)
