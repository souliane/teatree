"""Conformance test § 5.2: every ``ProvisionStep`` carries a post-condition or idempotent marker.

The forensic provisioning root-cause analysis
(``docs/provisioning-rootcause-2026-05-27.md``) identifies Pattern B: a
step's ``callable`` returns without raising, but the real artifact is
broken (symlink dangling, env file unreadable, DB dropped, node_modules
empty). The FSM reports green because the runner only checks "did the
callable raise". This test is the falsification experiment for that
pattern. If it passes on ``main`` without code changes → paradigm-mismatch
overstated. If RED → the typed-DAG-node ``ProvisionStep`` move (paradigm
issue) is the exit, because the contract today has no ``post_condition``
or ``idempotent`` fields at all.
"""

import logging
from dataclasses import fields

import pytest

from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_all_overlays
from teatree.types import ProvisionStep

logger = logging.getLogger(__name__)


def _step_fields() -> set[str]:
    return {f.name for f in fields(ProvisionStep)}


def _collect_steps(overlay: OverlayBase) -> list[tuple[str, ProvisionStep]]:
    """Return ``(source_hook, step)`` pairs from every step-emitting hook."""
    collected: list[tuple[str, ProvisionStep]] = []
    for hook_name in ("get_provision_steps", "provisioning.post_db_steps", "provisioning.cleanup_steps"):
        hook = getattr(overlay, hook_name, None)
        if hook is None:
            continue
        try:
            steps = hook(None)
        except Exception:
            logger.debug("Hook %s on overlay raised during step collection", hook_name, exc_info=True)
            continue
        collected.extend((hook_name, step) for step in steps or [] if isinstance(step, ProvisionStep))

    reset_hook = getattr(overlay, "provisioning.reset_passwords_command", None)
    if reset_hook is not None:
        try:
            step = reset_hook(None)
        except Exception:
            logger.debug("provisioning.reset_passwords_command raised during step collection", exc_info=True)
            step = None
        if isinstance(step, ProvisionStep):
            collected.append(("provisioning.reset_passwords_command", step))

    return collected


@pytest.fixture(scope="module")
def registered_overlays() -> dict[str, OverlayBase]:
    return get_all_overlays()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Documents the open paradigm-mismatch claim (#1386): the ProvisionStep "
        "dataclass has no post_condition or idempotent fields. Goes RED on "
        "main on purpose — XPASS once #1386 lands will force this marker off."
    ),
)
def test_provision_step_dataclass_declares_post_condition_and_idempotent_fields() -> None:
    """Require typed ``post_condition`` and ``idempotent`` fields on ``ProvisionStep``.

    Today the dataclass has ``name`` / ``callable`` / ``required`` /
    ``description`` only. This assertion goes RED on ``main`` — see
    ``docs/provisioning-rootcause-2026-05-27.md`` § 3.2.

    Marked ``xfail(strict=True)``: it WILL fail until paradigm issue
    [#1386](https://github.com/souliane/teatree/issues/1386) lands the
    typed-DAG-node move.
    """
    field_names = _step_fields()
    missing = {"post_condition", "idempotent"} - field_names
    assert not missing, (
        "ProvisionStep is missing typed contract fields: "
        + ", ".join(sorted(missing))
        + f". Current fields: {sorted(field_names)}. "
        "See docs/provisioning-rootcause-2026-05-27.md § 3.2."
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Documents the open paradigm-mismatch claim (#1386): no ProvisionStep "
        "instance can carry a post_condition because the dataclass lacks the "
        "field. Goes RED on main on purpose — XPASS once #1386 lands will "
        "force this marker off."
    ),
)
def test_every_provision_step_carries_post_condition_or_idempotent_marker(
    registered_overlays: dict[str, OverlayBase],
) -> None:
    """Falsify Pattern B: a step without a post-condition probe cannot prove its artifact.

    This test goes RED on ``main`` because ``ProvisionStep`` today has no
    such fields at all (see prerequisite test above). Once § 3.2 lands,
    this test gains teeth — it asserts per-instance carrying.

    Marked ``xfail(strict=True)``: it WILL fail until paradigm issue
    [#1386](https://github.com/souliane/teatree/issues/1386) lands.
    """
    assert registered_overlays, "no overlays registered — cannot validate the contract"

    step_fields = _step_fields()
    if "post_condition" not in step_fields or "idempotent" not in step_fields:
        pytest.fail(
            "ProvisionStep lacks post_condition / idempotent fields entirely — "
            "every step is implicitly Pattern B. "
            "See docs/provisioning-rootcause-2026-05-27.md § 3.2."
        )

    violations = [
        f"{overlay_name}.{source_hook} → {step.name}"
        for overlay_name, overlay in sorted(registered_overlays.items())
        for source_hook, step in _collect_steps(overlay)
        if getattr(step, "post_condition", None) is None and not getattr(step, "idempotent", False)
    ]

    assert not violations, (
        "ProvisionStep instances missing post_condition AND not idempotent=True "
        "(Pattern B — callable-return success masks broken artifact):\n  " + "\n  ".join(violations)
    )
