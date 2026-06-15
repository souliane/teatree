"""Discover eval scenarios shipped with teatree core and overlays.

Three surfaces are walked, in order:

1.  The core catalog at ``evals/scenarios/*.yaml`` — cross-
    overlay invariants whose fixtures use placeholder identities.
2.  Co-located ``skills/<name>/evals.yaml`` siblings — a skill ships its
    own behavioral evals beside its ``SKILL.md`` (the Anthropic skill-
    authoring convention). A co-located spec defaults its ``agent_path``
    to its owning ``skills/<name>/SKILL.md`` when it omits one.
3.  Each installed overlay's ``get_eval_scenarios_dir()`` hook
    (see :class:`teatree.core.overlay.OverlayBase`). Overlay-specific
    scenarios that reference tenant identities, banned-jargon lists, or
    per-workspace channel ids live in the overlay package so the core
    catalog remains overlay-agnostic.

Discovery is best-effort with respect to overlay failures: a broken
overlay (import error, missing directory) is skipped with a debug log
rather than failing the whole catalog. This mirrors
``teatree.core.overlay_loader.infer_overlay_for_url`` which uses the
same isolation discipline.

Scenario names are unique across all three surfaces: a collision is a
hard :class:`~teatree.eval.loader.EvalSpecError` surfaced at discovery so
``t3 eval run <name>`` can never resolve ambiguously.
"""

import logging
from pathlib import Path

from teatree.eval.loader import EvalSpecError, load_eval_yaml
from teatree.eval.models import EvalSpec

logger = logging.getLogger(__name__)


class ScenarioCatalogError(RuntimeError):
    """Raised when the core scenario catalog directory does not exist.

    ``SCENARIOS_DIR.glob("*.yaml")`` returns ``[]`` (no raise) on a missing dir,
    so a mis-pointed move would silently shrink the catalog to the handful of
    co-located specs while a metered run still meters ``>$0`` and exits green.
    A missing catalog dir is a hard configuration error, not an empty catalog.
    """


SCENARIOS_DIR = Path(__file__).resolve().parents[3] / "evals" / "scenarios"
# ``skills/`` sits next to ``src/`` in the teatree tree; resolve it from this
# module's path so discovery stays a leaf of the eval package (the same
# backwards-edge convention ``trigger_qa`` follows — it must not reach up into
# ``teatree.skill_support.loading``, a higher-level module).
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"


def discover_specs() -> list[EvalSpec]:
    if not SCENARIOS_DIR.is_dir():
        msg = (
            f"scenario catalog directory is missing: {SCENARIOS_DIR}. A missing dir would yield an "
            "empty catalog (glob returns []), silently shrinking the suite. Check the path / the move."
        )
        raise ScenarioCatalogError(msg)
    specs: list[EvalSpec] = []
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        specs.extend(load_eval_yaml(path))
    specs.extend(_discover_colocated_specs(skills_dir=DEFAULT_SKILLS_DIR))
    specs.extend(_discover_overlay_specs())
    _reject_duplicate_names(specs)
    return specs


def _reject_duplicate_names(specs: list[EvalSpec]) -> None:
    seen: dict[str, Path] = {}
    for spec in specs:
        first = seen.get(spec.name)
        if first is not None:
            raise EvalSpecError(
                spec.source_path,
                None,
                f"duplicate scenario name {spec.name!r} (also defined in {first})",
            )
        seen[spec.name] = spec.source_path


def _discover_colocated_specs(*, skills_dir: Path = DEFAULT_SKILLS_DIR) -> list[EvalSpec]:
    if not skills_dir.is_dir():
        return []
    specs: list[EvalSpec] = []
    for evals_yaml in sorted(skills_dir.glob("*/evals.yaml")):
        owning_skill = evals_yaml.parent.name
        default_agent_path = f"skills/{owning_skill}/SKILL.md"
        specs.extend(load_eval_yaml(evals_yaml, default_agent_path=default_agent_path))
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
