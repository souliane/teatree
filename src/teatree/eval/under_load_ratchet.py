"""The shrink-only ratchet over the metered ``under_load`` known-red baseline.

The ``under_load`` behavioural-drift lane sends the model the FULL skill bundle
under realistic context pressure and measures whether a cross-cutting invariant
survives. Several scenarios drift; the skill-prose fixes are necessary but a
prose edit does NOT prove the model's behaviour flipped — only a metered run
does, and the behavioural fix to turn them actually green is DEFERRED. So the
lane RATCHETS the known-red set instead of gating on a clean green.

This is the architectural-fitness-function counterpart of the deferred-import
ratchet / the FF-naming baseline: a checked-in ``evals/under_load_known_red.yaml``
freezes the currently-failing scenarios, and :func:`check_under_load_ratchet`
enforces two directions in one pass against a metered run's failing set:

* ``REGRESSION`` — an ``under_load`` scenario FAILED that is NOT in the baseline.
A NEW red beyond the baseline is a real regression; the gate goes RED.
* ``STALE`` — a baselined scenario is NOT failing (it passed). The set may only
SHRINK, so a now-passing scenario must be REMOVED from the file; leaving it in
(or re-adding it) fails the gate. This forces the eventual behavioural fix to
drive the baseline toward zero and forbids back-sliding.

The config is checked in, so the baseline survives a DB reset and every change to
it is reviewed in a diff. An absent file is a configuration error (it would make
the ratchet vacuously green), not an empty baseline.
"""

import dataclasses
import enum
from collections.abc import Iterable, Mapping
from pathlib import Path

import yaml

#: The committed baseline file, resolved from this module's path so the eval
#: package stays a leaf (the same convention ``cost_bounds.COST_BOUNDS_PATH`` and
#: ``discovery.SCENARIOS_DIR`` follow).
UNDER_LOAD_KNOWN_RED_PATH = Path(__file__).resolve().parents[3] / "evals" / "under_load_known_red.yaml"


class UnderLoadRatchetError(ValueError):
    """A malformed ``under_load_known_red.yaml`` — a missing file, a non-list, etc."""


@dataclasses.dataclass(frozen=True, slots=True)
class UnderLoadKnownRed:
    """The frozen, shrink-only baseline of currently-known-red under_load scenarios."""

    known_red: frozenset[str]


class UnderLoadViolationKind(enum.Enum):
    #: A NON-baselined under_load scenario failed — a real regression beyond the baseline.
    REGRESSION = "regression"
    #: A baselined scenario is no longer failing — it must be REMOVED (shrink-only).
    STALE = "stale"


@dataclasses.dataclass(frozen=True, slots=True)
class UnderLoadViolation:
    """One scenario that breaks the ratchet — a new red, or a baseline entry that now passes."""

    scenario_name: str
    kind: UnderLoadViolationKind

    def render(self) -> str:
        if self.kind is UnderLoadViolationKind.REGRESSION:
            return (
                f"UNDER_LOAD REGRESSION {self.scenario_name}: FAILED but is NOT in the "
                "known-red baseline (evals/under_load_known_red.yaml). A new red beyond the "
                "baseline is a real regression — fix the drift, do NOT widen the baseline."
            )
        return (
            f"UNDER_LOAD STALE BASELINE {self.scenario_name}: in the known-red baseline "
            "but it is NOW PASSING. The baseline is shrink-only — REMOVE this entry from "
            "evals/under_load_known_red.yaml so the set can only shrink toward zero."
        )


@dataclasses.dataclass(frozen=True, slots=True)
class UnderLoadRatchetResult:
    """The outcome of ratcheting a metered run's failing under_load set against the baseline."""

    violations: tuple[UnderLoadViolation, ...]
    baseline_size: int

    @property
    def failed(self) -> bool:
        return bool(self.violations)


def load_under_load_known_red(path: Path | None = None) -> UnderLoadKnownRed:
    """Parse ``under_load_known_red.yaml`` into a typed :class:`UnderLoadKnownRed`.

    Raises :class:`UnderLoadRatchetError` on a malformed file (so a typo is a hard
    RED at gate time, never a silently-dropped baseline entry). A missing file is a
    configuration error, not an empty baseline — an absent baseline would make the
    ratchet vacuously green (no regression could ever be detected).
    """
    baseline_path = path or UNDER_LOAD_KNOWN_RED_PATH
    if not baseline_path.is_file():
        msg = (
            f"under_load known-red baseline is missing: {baseline_path}. An absent file would "
            "make the shrink-only ratchet vacuously green. Check the path / the move."
        )
        raise UnderLoadRatchetError(msg)
    loaded = yaml.safe_load(baseline_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping):
        msg = f"{baseline_path}: expected a top-level mapping with a 'known_red' list"
        raise UnderLoadRatchetError(msg)
    raw = loaded.get("known_red")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        msg = f"{baseline_path}: 'known_red' must be a list of scenario names, got {type(raw).__name__}"
        raise UnderLoadRatchetError(msg)
    names: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            msg = f"{baseline_path}: each 'known_red' entry must be a scenario name string, got {entry!r}"
            raise UnderLoadRatchetError(msg)
        names.append(entry)
    if len(names) != len(set(names)):
        dupes = sorted({n for n in names if names.count(n) > 1})
        msg = f"{baseline_path}: duplicate 'known_red' entries {dupes!r} — each scenario appears at most once"
        raise UnderLoadRatchetError(msg)
    return UnderLoadKnownRed(known_red=frozenset(names))


def check_under_load_ratchet(
    failing_under_load: Iterable[str],
    passing_under_load: Iterable[str],
    baseline: UnderLoadKnownRed,
) -> UnderLoadRatchetResult:
    """Ratchet a metered run's under_load verdicts against the baseline.

    ``failing_under_load`` is the set of under_load scenario names that RAN and
    whose authoritative pass@k verdict is FAIL (``PassAtKResult.ok is False``).
    ``passing_under_load`` is the set that RAN and PASSED (``ok is True``). A
    SKIPPED scenario (no key / no execution) is in NEITHER set — it is not evidence
    of a pass or a fail, so it can never trip the gate (a key-less all-skipped run
    is not spuriously a regression or a stale-baseline failure).

    The contract, both directions in one pass:

    * a *failing* scenario NOT in the baseline → a ``REGRESSION`` violation (a new
    red beyond the baseline);
    * a baselined scenario that *passed* → a ``STALE`` violation (shrink-only: it
    must be removed from the baseline).

    :attr:`~UnderLoadRatchetResult.failed` is ``True`` when any violation exists.
    A run whose executed failing set EQUALS the baseline (and nothing baselined
    passed) passes — documented known-red, no regression, nothing to shrink.
    """
    failing = set(failing_under_load)
    passing = set(passing_under_load)
    regressions = [
        UnderLoadViolation(scenario_name=name, kind=UnderLoadViolationKind.REGRESSION)
        for name in sorted(failing - baseline.known_red)
    ]
    stale = [
        UnderLoadViolation(scenario_name=name, kind=UnderLoadViolationKind.STALE)
        for name in sorted(baseline.known_red & passing)
    ]
    return UnderLoadRatchetResult(violations=tuple(regressions + stale), baseline_size=len(baseline.known_red))
