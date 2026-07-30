"""The full-suite eval GREEN PROOF — assert a merged eval-heal JSON is red-free (#3202).

The CI heal workflow shards the full behavioral-eval suite across a parallel
matrix and folds every shard's publish-safe per-scenario JSON into ONE
``eval-heal-<sha>`` payload (:mod:`teatree.eval.summary_json_merge`). That merged
JSON is the PROOF the full suite is green: every scenario carries the derived
``triage_class`` (:func:`teatree.eval.triage.classify_red`) the ``--summary-json``
producer already embedded, so a red — behavioral, any ``infra_*``, ``judge``, or a
``no_coverage`` skip — is exactly a scenario with a NON-null ``triage_class``.

This is the eval-heal workflow's SECOND gating step — the shards gate on their own
``t3 eval run`` exit code, then the combine job gates again here — so the
interactive-surface exemption the in-process lanes apply has to hold here too, or
a bundled-CLI rendering change reds the combine job after every shard passed
(souliane/teatree#3855, souliane/teatree#3921). An ``advisory`` row is therefore
reported but never withholds the proof.

:func:`evaluate_green_proof` reads that one payload and decides: a proof holds iff
the run actually EXECUTED scenarios (``total > 0`` — an empty artifact is not a
proof, closing the all-skipped-masquerades-as-green hole) AND carries ZERO GATING
reds.
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
    """The verdict of one merged eval-heal run: its totals, its gating reds, its advisory reds."""

    total: int
    passed: int
    failed: int
    skipped: int
    reds: tuple[RedScenario, ...]
    advisory: tuple[RedScenario, ...] = ()

    @property
    def is_green(self) -> bool:
        """A proof holds iff the run EXECUTED scenarios and recorded zero GATING reds.

        ``total == 0`` is NOT green: an empty / all-skipped artifact proves nothing,
        so a run that eval'd nothing can never masquerade as the full-suite proof.
        :attr:`advisory` rows are excluded from :attr:`reds` by :func:`_partition`,
        so they are reported here without ever withholding the proof.
        """
        return self.total > 0 and not self.reds

    @property
    def summary(self) -> str:
        headline = f"GREEN PROOF: {self.passed}/{self.total} passed, 0 reds" if self.is_green else self._red_headline()
        lines = [headline]
        lines.extend(f"  RED {red.name} [{red.lane}] -> {red.triage_class}" for red in self.reds)
        if self.advisory:
            lines.append(f"  {len(self.advisory)} advisory (reported, non-gating):")
            lines.extend(f"    ADVISORY {row.name} [{row.lane}] -> {row.triage_class}" for row in self.advisory)
        return "\n".join(lines)

    def _red_headline(self) -> str:
        if self.total == 0:
            return "NOT A GREEN PROOF: the merged run executed 0 scenarios (nothing to prove)"
        return (
            f"NOT A GREEN PROOF: {len(self.reds)} red scenario(s) "
            f"({self.passed}/{self.total} passed, {self.failed} failed, {self.skipped} skipped)"
        )


def _partition(scenarios: Sequence[Any]) -> tuple[tuple[RedScenario, ...], tuple[RedScenario, ...]]:
    """Split the non-null-``triage_class`` rows into (gating reds, advisory reds).

    An ``advisory`` row is an ``interactive``-surface scenario: graded, classified
    and REPORTED exactly like any other red, but never gating, because its verdict
    rides a bundled claude CLI's ``AskUserQuestion`` rendering rather than the
    question contract teatree owns (souliane/teatree#3855). Both the flag and the
    surface are written by the ``--summary-json`` producer; a row missing the flag
    (an artifact from before souliane/teatree#3921) reads as GATING, so an older
    payload can never be silently exempted.
    """
    reds: list[RedScenario] = []
    advisory: list[RedScenario] = []
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            continue
        triage_class = scenario.get("triage_class")
        if triage_class is None:
            continue
        row = RedScenario(
            name=str(scenario.get("name", "")),
            lane=str(scenario.get("lane", "")),
            triage_class=str(triage_class),
        )
        (advisory if bool(scenario.get("advisory")) else reds).append(row)
    return tuple(reds), tuple(advisory)


def evaluate_green_proof(payload: Mapping[str, Any]) -> GreenProof:
    """Read a merged §2.4 ``eval-heal`` payload and return its :class:`GreenProof`."""
    totals = payload.get("totals")
    totals = totals if isinstance(totals, Mapping) else {}
    scenarios = payload.get("scenarios")
    scenarios = scenarios if isinstance(scenarios, list) else []
    reds, advisory = _partition(scenarios)
    return GreenProof(
        total=int(totals.get("total", 0)),
        passed=int(totals.get("passed", 0)),
        failed=int(totals.get("failed", 0)),
        skipped=int(totals.get("skipped", 0)),
        reds=reds,
        advisory=advisory,
    )
