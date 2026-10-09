"""Shared helpers to PIN the active posture for a test that depends on it.

``Mode.egress`` is the only control over the owner's colleague-visible voice, read at the
``_BaseReplier`` chokepoint through
:func:`teatree.core.mode_resolution.owner_voice_forbidden`. Tests that exercise transport
*mechanics* — idempotency, status recording, backend wiring — need the posture to PERMIT so
their assertions still hold; the posture's own decision has dedicated suites.

The pin runs BOTH ways. ``owner_voice_forbidden`` fails CLOSED, so a database with no seeded
``Mode`` row already refuses — a test about the chokepoint REFUSING still pins that direction
explicitly, so the case states what it exercises and survives a later change of shipped rows.

The pin replaces the name where the chokepoint READS it rather than seeding a ``Mode`` row: it
needs no database, so it serves every caller, and no other test's override can undo it.

A posture is only HALF the precondition on the review surface, where the post names its target
repo: ``on_behalf_auto_actions`` is read per-overlay ahead of the posture, and a target no
overlay owns has no overlay tier of its own — inheriting the ambient overlay's is the
mis-attribution the gate closes. :data:`OWNED_REPO` supplies the other half.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest

_POSTURE_SEAM = "teatree.core.on_behalf_gate_recorded.owner_voice_forbidden"

#: A review target the gate can read an allowlist FOR — owned by ``t3-teatree``, the
#: in-repo overlay ``tests/conftest.py`` pins (its ``get_workspace_repos()`` lists ``teatree``).
OWNED_REPO = "souliane/teatree"


@contextmanager
def posture_permits_cm() -> Iterator[None]:
    """Context manager: the active posture lets the owner's voice out inside the block."""
    with patch(_POSTURE_SEAM, return_value=False):
        yield


@contextmanager
def posture_forbids_cm() -> Iterator[None]:
    """Context manager: the active posture refuses colleague-visible posts inside the block."""
    with patch(_POSTURE_SEAM, return_value=True):
        yield


def seed_permitting_posture() -> None:
    """Seed and select a posture that lets the owner's voice out. Needs the database.

    The DB-backed sibling of :func:`posture_permits_cm`: it exercises the resolution layer
    that decides rather than replacing it, so a suite about the gate itself stages through
    the same rows production reads.
    """
    _seed_posture("present", "allow")


def seed_forbidding_posture() -> None:
    """Seed and select a posture that refuses the owner's voice. Needs the database.

    Pinned rather than left to the fail-closed default, so the case states the posture it
    exercises and survives a change to what a fresh database resolves.
    """
    _seed_posture("afk", "forbid")


def _seed_posture(name: str, egress: str) -> None:
    from teatree.core.models import Mode, ModeOverride  # noqa: PLC0415 — deferred: ORM needs the app registry

    Mode.objects.update_or_create(name=name, defaults={"entries": {}, "egress": egress})
    ModeOverride.objects.set_override(name, reason=f"{egress} posture for this test")


def disable_on_behalf_gate(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the posture to PERMIT for this test.

    Designed for autouse fixtures that open the gate for the lifetime of a class- or
    function-scoped test: ``monkeypatch`` restores the real resolver at teardown, so the
    fail-closed default returns automatically.
    """
    permit_on_behalf_gate(monkeypatch)


def arm_on_behalf_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-pin the posture to REFUSE, undoing an autouse :func:`disable_on_behalf_gate`.

    A suite-wide gate-off is a patched resolver, not an environment value, so a case that
    needs the gate armed has to replace the patch rather than clear a variable.
    """
    monkeypatch.setattr(_POSTURE_SEAM, lambda *_args, **_kwargs: True)


def permit_on_behalf_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the posture to PERMIT for the lifetime of the test."""
    monkeypatch.setattr(_POSTURE_SEAM, lambda *_args, **_kwargs: False)
