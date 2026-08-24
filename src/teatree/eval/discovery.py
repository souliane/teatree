"""Discover eval scenarios shipped with teatree core and overlays.

Two surfaces are walked, in order:

1.  The core catalog at ``evals/scenarios/*.yaml`` — the single canonical
    home for every shipped scenario. A scenario targeting a skill carries an
    explicit ``agent_path: skills/<name>/SKILL.md`` (coverage is attributed
    per skill through that path). Scenario bodies never live co-located inside
    the ``skills/`` tree — that tree carries skill prose only
    (``tests/eval_replay/test_no_inline_skill_evals.py`` enforces it).
2.  Each installed overlay's ``get_eval_scenarios_dir()`` hook
    (see :class:`teatree.core.overlay.OverlayBase`). Overlay-specific
    scenarios that reference tenant identities, banned-jargon lists, or
    per-workspace channel ids live in the overlay package so the core
    catalog remains overlay-agnostic. The hook returns ``None`` to contribute
    nothing, or a directory that EXISTS — an overlay must not launder a moved
    directory into ``None``, because only one of those two is legitimate and
    the catalog cannot otherwise tell them apart.

Discovery is best-effort with respect to overlay failures: a broken
overlay (import error, missing directory) is skipped rather than failing
the whole catalog. This mirrors
``teatree.core.overlay_loader.infer_overlay_for_url`` which uses the
same isolation discipline.

Best-effort is not silent. Every skipped overlay is logged at WARNING and
recorded on the catalog (:func:`discover_catalog`), because a fallback that
hides its own primary failure produces the shape every other guard here is
built to refuse: a run that meters real spend, executes scenarios, and reports
green while the overlay half of the suite — tenant identities, banned-jargon
lists — silently stopped being evaluated.

Scenario names are unique across all three surfaces: a collision is a
hard :class:`~teatree.eval.loader.EvalSpecError` surfaced at discovery so
``t3 eval run <name>`` can never resolve ambiguously.
"""

import dataclasses
import logging
from pathlib import Path

from teatree.eval.loader import EvalSpecError, load_eval_yaml
from teatree.eval.models import EvalSpec

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ScenarioCatalog:
    """The discovered scenarios plus every overlay surface that failed to contribute.

    ``degraded`` is the reason-per-overlay map for surfaces that raised OR that
    succeeded while naming a directory that is not there: an entry means the catalog
    is SMALLER than the tree defines, which no downstream guard can detect
    (``--require-executed`` still sees executions, a metered run still meters spend).
    ``"*"`` keys the whole-registry failure, where not even the overlay names are known.

    ``core_count`` is the shipped-catalog half of the same question. Degradation names
    a surface that vanished; the count is what :data:`CORE_CATALOG_FLOOR` is measured
    against, so the core catalog cannot shrink without an edit either.
    """

    specs: list[EvalSpec]
    degraded: dict[str, str]
    core_count: int

    @property
    def is_complete(self) -> bool:
        return not self.degraded


class ScenarioCatalogError(RuntimeError):
    """Raised when the core scenario catalog directory does not exist.

    ``SCENARIOS_DIR.glob("*.yaml")`` returns ``[]`` (no raise) on a missing dir,
    so a mis-pointed move would silently shrink the catalog to whatever the
    installed overlays contribute while a metered run still meters ``>$0`` and
    exits green. A missing catalog dir is a hard configuration error, not an
    empty catalog.
    """


SCENARIOS_DIR = Path(__file__).resolve().parents[3] / "evals" / "scenarios"
# ``skills/`` sits next to ``src/`` in the teatree tree; resolve it from this
# module's path so the eval package stays a leaf (the same backwards-edge
# convention ``coverage`` follows — it must not reach up into
# ``teatree.skill_support.loading``, a higher-level module). The per-skill
# coverage gate (``teatree.eval.coverage``) enumerates skills from here.
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"

#: Shrink-only ratchet on the CORE catalog — raise it when scenarios are added,
#: and edit it deliberately when one is removed. Set at the shipped count rather
#: than a loose collapse-detector because #4373's denominator shrank by two, which
#: any slack at all hides. It floors the core surface alone: an overlay only ever
#: ADDS, so flooring the total would red an install contributing none.
CORE_CATALOG_FLOOR = 241


def discover_core_specs() -> list[EvalSpec]:
    """The shipped core catalog alone — the surface teatree's own structural guards may pin.

    An overlay's scenarios are the overlay's to pin, so a guard asserting a property
    of every shipped scenario reads this rather than the whole catalog.
    """
    if not SCENARIOS_DIR.is_dir():
        msg = (
            f"scenario catalog directory is missing: {SCENARIOS_DIR}. A missing dir would yield an "
            "empty catalog (glob returns []), silently shrinking the suite. Check the path / the move."
        )
        raise ScenarioCatalogError(msg)
    specs: list[EvalSpec] = []
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        specs.extend(load_eval_yaml(path))
    return specs


def discover_catalog() -> ScenarioCatalog:
    """The full catalog plus the overlay surfaces that failed to contribute to it."""
    specs = discover_core_specs()
    core_count = len(specs)
    degraded: dict[str, str] = {}
    specs.extend(_discover_overlay_specs(degraded))
    _reject_duplicate_names(specs)
    return ScenarioCatalog(specs=specs, degraded=degraded, core_count=core_count)


def discover_specs() -> list[EvalSpec]:
    return discover_catalog().specs


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


def find_spec(name: str) -> EvalSpec | None:
    for spec in discover_specs():
        if spec.name == name:
            return spec
    return None


def _discover_overlay_specs(degraded: dict[str, str]) -> list[EvalSpec]:
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: loaded per eval run

    specs: list[EvalSpec] = []
    try:
        overlays = get_all_overlays()
    except Exception as exc:
        logger.warning("eval-discovery: get_all_overlays() failed — no overlay scenario ran", exc_info=True)
        degraded["*"] = f"overlay registry unreadable: {type(exc).__name__}: {exc}"
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
                logger.warning("eval-discovery: overlay %r named a scenarios dir that is not there", name)
                degraded[name] = f"get_eval_scenarios_dir() named a directory that is not there: {scenarios_path}"
                continue
            yaml_paths = sorted(scenarios_path.glob("*.yaml"))
        except Exception as exc:
            logger.warning("eval-discovery: overlay %r get_eval_scenarios_dir() failed", name, exc_info=True)
            degraded[name] = f"get_eval_scenarios_dir() failed: {type(exc).__name__}: {exc}"
            continue
        for yaml_path in yaml_paths:
            try:
                specs.extend(load_eval_yaml(yaml_path))
            except Exception as exc:
                logger.warning("eval-discovery: overlay %r scenario %s failed to load", name, yaml_path, exc_info=True)
                degraded[name] = f"{yaml_path.name} failed to load: {type(exc).__name__}: {exc}"
    return specs
