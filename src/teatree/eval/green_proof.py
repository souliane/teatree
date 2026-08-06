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
the run COVERED the whole catalog (``total == expected_total`` and one row per
counted scenario) AND carries ZERO GATING reds.

Coverage is the load-bearing half. A shard whose leg died before uploading
contributes nothing to the merge, and the combine job runs anyway
(``if: always()``), so a payload folded from one surviving shard is internally
consistent and reads green — 231/231 was asserted only in the CI step's NAME.
The expected count is therefore passed in by the caller (the live catalog at the
eval'd sha) and the proof fails when the merged run covers less than it.

Pure and payload-only (no I/O, no DB), mirroring :mod:`teatree.eval.summary_json_merge`,
so it is unit-testable and the ``t3 eval green-proof`` CLI is a thin JSON-read shell.
"""

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

#: The triage class a row carries when the producer wrote none — gating by construction.
UNCLASSIFIED = "unclassified"


@dataclasses.dataclass(frozen=True)
class RedScenario:
    """One red scenario, read verbatim from the merged JSON — never a transcript."""

    name: str
    lane: str
    triage_class: str


@dataclasses.dataclass(frozen=True)
class GreenProof:
    """The verdict of one merged eval-heal run: its coverage, its gating reds, its advisory reds."""

    total: int
    passed: int
    failed: int
    skipped: int
    reds: tuple[RedScenario, ...]
    advisory: tuple[RedScenario, ...] = ()
    expected_total: int = 0
    rows: int = 0

    @property
    def covers_the_catalog(self) -> bool:
        """Whether the merged payload accounts for every scenario the catalog defines.

        Two independent reads must agree: the summed ``totals.total`` reaches the
        expected count, and the ``scenarios`` list carries one row per counted
        scenario (the producer writes exactly one row per result, so a shortfall
        means the payload was truncated after its totals were summed).
        """
        return self.expected_total > 0 and self.total >= self.expected_total and self.rows == self.total

    @property
    def is_green(self) -> bool:
        """A proof holds iff the run COVERED the catalog and recorded zero GATING reds.

        A short run is NOT green: an empty, all-skipped, or missing-shard artifact
        proves nothing about the scenarios it never carried, so it can never
        masquerade as the full-suite proof. :attr:`advisory` rows are excluded from
        :attr:`reds` by :func:`_partition`, so they are reported here without ever
        withholding the proof.
        """
        return self.covers_the_catalog and not self.reds

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
        if not self.covers_the_catalog:
            return (
                f"NOT A GREEN PROOF: the merged run covered {self.total} scenario(s) "
                f"({self.rows} row(s)) of the {self.expected_total} the catalog defines — "
                "a shard that never uploaded proves nothing about the scenarios it carried"
            )
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

    A row with no ``triage_class`` KEY at all is likewise GATING: the producer
    always writes the key (``null`` for a pass), so its absence means the row was
    not produced by a grader this gate understands — the same fail-closed
    asymmetry ``advisory`` applies.
    """
    reds: list[RedScenario] = []
    advisory: list[RedScenario] = []
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            reds.append(RedScenario(name="", lane="", triage_class=UNCLASSIFIED))
            continue
        if "triage_class" not in scenario:
            # Unclassified is GATING whatever the advisory flag says: the exemption
            # is an opt-in the grader writes alongside the class, so it can never
            # rescue a row whose grading this gate cannot read.
            reds.append(
                RedScenario(name=_str(scenario, "name"), lane=_str(scenario, "lane"), triage_class=UNCLASSIFIED)
            )
            continue
        triage_class = scenario["triage_class"]
        if triage_class is None:
            continue
        row = RedScenario(
            name=_str(scenario, "name"),
            lane=_str(scenario, "lane"),
            triage_class=str(triage_class),
        )
        (advisory if bool(scenario.get("advisory")) else reds).append(row)
    return tuple(reds), tuple(advisory)


def _str(scenario: Mapping[str, Any], key: str) -> str:
    return str(scenario.get(key, ""))


def evaluate_green_proof(payload: Mapping[str, Any], *, expected_total: int) -> GreenProof:
    """Read a merged §2.4 ``eval-heal`` payload and return its :class:`GreenProof`.

    *expected_total* is how many scenarios the catalog at the eval'd sha defines;
    the proof is withheld unless the merged run covers all of them, so a lost
    shard can never shrink the suite into a green.
    """
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
        expected_total=expected_total,
        rows=len(scenarios),
    )
