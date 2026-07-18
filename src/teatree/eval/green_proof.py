"""The full-suite eval GREEN PROOF — assert a merged eval-heal JSON is red-free (#3202).

The CI heal workflow shards the full behavioral-eval suite across a parallel
matrix and folds every shard's publish-safe per-scenario JSON into ONE
``eval-heal-<sha>`` payload (:mod:`teatree.eval.summary_json_merge`). That merged
JSON is the PROOF the full suite is green: every scenario carries the derived
``triage_class`` (:func:`teatree.eval.triage.classify_red`) the ``--summary-json``
producer already embedded, so a red — behavioral, any ``infra_*``, ``judge``, or a
``no_coverage`` skip — is exactly a scenario with a NON-null ``triage_class``.

:func:`evaluate_green_proof` reads that one payload and decides: a proof holds iff
the run actually EXECUTED scenarios (``total > 0`` — an empty artifact is not a
proof, closing the all-skipped-masquerades-as-green hole) AND carries ZERO reds.
Pure and payload-only (no I/O, no DB), mirroring :mod:`teatree.eval.summary_json_merge`,
so it is unit-testable and the ``t3 eval green-proof`` CLI is a thin JSON-read shell.
"""

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any


@dataclasses.dataclass(frozen=True)
class RedScenario:
    """One red scenario, read verbatim from the merged JSON — never a transcript."""

    name: str
    lane: str
    triage_class: str


@dataclasses.dataclass(frozen=True)
class GreenProof:
    """The verdict of one merged eval-heal run: its totals plus every red scenario."""

    total: int
    passed: int
    failed: int
    skipped: int
    reds: tuple[RedScenario, ...]

    @property
    def is_green(self) -> bool:
        """A proof holds iff the run EXECUTED scenarios and recorded zero reds.

        ``total == 0`` is NOT green: an empty / all-skipped artifact proves nothing,
        so a run that eval'd nothing can never masquerade as the full-suite proof.
        """
        return self.total > 0 and not self.reds

    @property
    def summary(self) -> str:
        headline = f"GREEN PROOF: {self.passed}/{self.total} passed, 0 reds" if self.is_green else self._red_headline()
        lines = [headline]
        lines.extend(f"  RED {red.name} [{red.lane}] -> {red.triage_class}" for red in self.reds)
        return "\n".join(lines)

    def _red_headline(self) -> str:
        if self.total == 0:
            return "NOT A GREEN PROOF: the merged run executed 0 scenarios (nothing to prove)"
        return (
            f"NOT A GREEN PROOF: {len(self.reds)} red scenario(s) "
            f"({self.passed}/{self.total} passed, {self.failed} failed, {self.skipped} skipped)"
        )


def _reds(scenarios: Sequence[Any]) -> tuple[RedScenario, ...]:
    """Every scenario carrying a non-null ``triage_class`` — the reds, verbatim."""
    reds: list[RedScenario] = []
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            continue
        triage_class = scenario.get("triage_class")
        if triage_class is None:
            continue
        reds.append(
            RedScenario(
                name=str(scenario.get("name", "")),
                lane=str(scenario.get("lane", "")),
                triage_class=str(triage_class),
            )
        )
    return tuple(reds)


def evaluate_green_proof(payload: Mapping[str, Any]) -> GreenProof:
    """Read a merged §2.4 ``eval-heal`` payload and return its :class:`GreenProof`."""
    totals = payload.get("totals")
    totals = totals if isinstance(totals, Mapping) else {}
    scenarios = payload.get("scenarios")
    scenarios = scenarios if isinstance(scenarios, list) else []
    return GreenProof(
        total=int(totals.get("total", 0)),
        passed=int(totals.get("passed", 0)),
        failed=int(totals.get("failed", 0)),
        skipped=int(totals.get("skipped", 0)),
        reds=_reds(scenarios),
    )
