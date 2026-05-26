"""Discover eval scenarios shipped with teatree core and overlays.

Two surfaces are walked, in order:

1.  The core catalog at ``src/teatree/eval/scenarios/*.yaml`` — cross-
    overlay invariants whose fixtures use placeholder identities.
2.  Each installed overlay's ``get_eval_scenarios_dir()`` hook
    (see :class:`teatree.core.overlay.OverlayBase`). Overlay-specific
    scenarios that reference tenant identities, banned-jargon lists, or
    per-workspace channel ids live in the overlay package so the core
    catalog remains overlay-agnostic.

Discovery is best-effort with respect to overlay failures: a broken
overlay (import error, missing directory) is skipped with a debug log
rather than failing the whole catalog. This mirrors
``teatree.core.overlay_loader.infer_overlay_for_url`` which uses the
same isolation discipline.
"""

import logging
from pathlib import Path

from teatree.eval.loader import load_eval_yaml
from teatree.eval.models import EvalSpec

logger = logging.getLogger(__name__)

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def discover_specs() -> list[EvalSpec]:
    specs: list[EvalSpec] = []
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        specs.extend(load_eval_yaml(path))
    specs.extend(_discover_overlay_specs())
    return specs


def find_spec(name: str) -> EvalSpec | None:
    for spec in discover_specs():
        if spec.name == name:
            return spec
    return None


def _discover_overlay_specs() -> list[EvalSpec]:
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

    specs: list[EvalSpec] = []
    try:
        overlays = get_all_overlays()
    except Exception:
        logger.debug("eval-discovery: get_all_overlays() failed", exc_info=True)
        return specs
    for name, overlay in overlays.items():
        getter = getattr(overlay, "get_eval_scenarios_dir", None)
        if not callable(getter):
            continue
        try:
            scenarios_dir = getter()
            if scenarios_dir is None:
                continue
            scenarios_path = Path(scenarios_dir)
            if not scenarios_path.is_dir():
                continue
            yaml_paths = sorted(scenarios_path.glob("*.yaml"))
        except Exception:
            logger.debug(
                "eval-discovery: overlay %r get_eval_scenarios_dir() failed",
                name,
                exc_info=True,
            )
            continue
        for yaml_path in yaml_paths:
            try:
                specs.extend(load_eval_yaml(yaml_path))
            except Exception:
                logger.warning(
                    "eval-discovery: overlay %r scenario %s failed to load",
                    name,
                    yaml_path,
                    exc_info=True,
                )
    return specs
