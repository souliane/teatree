"""Verify one row per catalog scenario and version at the evaluated commit.

The weekly and eval-heal combine jobs use this pure gate after merging shard
summaries. A passing proof needs the exact catalog identity set, a matching SHA,
consistent totals and no gating red. Retry recoveries are FLAKY, never PASS.
Interactive behavioral reds remain visible but advisory; missing grading and
infrastructure failures always block.
"""

import dataclasses
from collections import Counter
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
    expected: Mapping[str, str] = dataclasses.field(default_factory=dict)
    expected_sha: str = ""
    actual_sha: str = ""
    identities: tuple[tuple[str, str], ...] = ()
    verdict_counts: Mapping[str, int] = dataclasses.field(default_factory=dict)
    rows: int = 0
    outcome_counts: Mapping[str, int] = dataclasses.field(default_factory=dict)

    @property
    def covers_the_catalog(self) -> bool:
        """Whether IDs, versions, SHA, uniqueness and totals match the catalog."""
        actual = Counter(self.identities)
        expected = Counter((name, version) for name, version in self.expected.items())
        return (
            bool(expected)
            and bool(self.expected_sha)
            and self.expected_sha == self.actual_sha
            and actual == expected
            and self.rows == self.total
            and self.total == self.passed + self.failed + self.skipped
            and self.passed == self.verdict_counts.get("pass", 0)
            and self.failed == self.verdict_counts.get("fail", 0)
            and self.skipped == self.verdict_counts.get("skip", 0)
        )

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
        counts = self.outcome_counts
        status = (
            f"{counts.get('PASS', 0)} PASS, {counts.get('FLAKY', 0)} FLAKY, "
            f"{counts.get('BEHAVIOR_FAIL', 0)} BEHAVIOR_FAIL, "
            f"{counts.get('INFRA_BLOCKED', 0)} INFRA_BLOCKED, "
            f"{counts.get('UNVERIFIED', 0)} UNVERIFIED over the exact selection at {self.actual_sha}"
        )
        headline = f"GREEN PROOF: {status}" if self.is_green else f"NOT A GREEN PROOF: {status}"
        lines = [headline]
        lines.extend(f"  RED {red.name} [{red.lane}] -> {red.triage_class}" for red in self.reds)
        if self.advisory:
            lines.append(f"  {len(self.advisory)} advisory (reported, non-gating):")
            lines.extend(f"    ADVISORY {row.name} [{row.lane}] -> {row.triage_class}" for row in self.advisory)
        if not self.covers_the_catalog:
            lines.append(self._red_headline())
        return "\n".join(lines)

    def _red_headline(self) -> str:
        if not self.covers_the_catalog:
            return (
                f"NOT A GREEN PROOF: the merged run covered {self.total} scenario(s) "
                f"({self.rows} row(s)) of {len(self.expected)} expected at {self.expected_sha}; "
                "IDs, versions, uniqueness, SHA and totals must all match"
            )
        return (
            f"NOT A GREEN PROOF: {len(self.reds)} red scenario(s) "
            f"({self.passed}/{self.total} passed, {self.failed} failed, {self.skipped} skipped)"
        )


def _partition(scenarios: Sequence[Any]) -> tuple[tuple[RedScenario, ...], tuple[RedScenario, ...]]:
    """Split the non-null-``triage_class`` rows into (gating reds, advisory reds).

    An interactive BEHAVIOR_FAIL remains advisory because its verdict depends on
    the bundled Claude CLI's question rendering. INFRA_BLOCKED and UNVERIFIED
    always gate, including for interactive scenarios. A missing advisory flag
    also gates.

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
        outcome = scenario.get("outcome")
        if outcome not in {"PASS", "FLAKY", "BEHAVIOR_FAIL", "INFRA_BLOCKED", "UNVERIFIED"}:
            reds.append(
                RedScenario(name=_str(scenario, "name"), lane=_str(scenario, "lane"), triage_class=UNCLASSIFIED)
            )
            continue
        verdict = scenario.get("verdict")
        if (outcome == "PASS" and verdict != "pass") or (outcome == "FLAKY" and verdict != "fail"):
            reds.append(
                RedScenario(name=_str(scenario, "name"), lane=_str(scenario, "lane"), triage_class=UNCLASSIFIED)
            )
            continue
        if outcome == "FLAKY":
            if scenario["triage_class"] is None:
                reds.append(
                    RedScenario(name=_str(scenario, "name"), lane=_str(scenario, "lane"), triage_class=UNCLASSIFIED)
                )
            continue
        triage_class = scenario["triage_class"]
        if triage_class is None and outcome == "PASS":
            continue
        if triage_class is None:
            triage_class = UNCLASSIFIED
        row = RedScenario(
            name=_str(scenario, "name"),
            lane=_str(scenario, "lane"),
            triage_class=str(triage_class),
        )
        (advisory if bool(scenario.get("advisory")) and outcome == "BEHAVIOR_FAIL" else reds).append(row)
    return tuple(reds), tuple(advisory)


def _str(scenario: Mapping[str, Any], key: str) -> str:
    return str(scenario.get(key, ""))


def evaluate_green_proof(payload: Mapping[str, Any], *, expected: Mapping[str, str], expected_sha: str) -> GreenProof:
    """Check a merged summary against the selected catalog at *expected_sha*."""
    totals = payload.get("totals")
    totals = totals if isinstance(totals, Mapping) else {}
    scenarios = payload.get("scenarios")
    scenarios = scenarios if isinstance(scenarios, list) else []
    reds, advisory = _partition(scenarios)
    identities = tuple((_str(row, "name"), _str(row, "version")) for row in scenarios if isinstance(row, Mapping))
    counts = Counter(_str(row, "outcome") for row in scenarios if isinstance(row, Mapping))
    verdict_counts = Counter(_str(row, "verdict") for row in scenarios if isinstance(row, Mapping))
    return GreenProof(
        total=int(totals.get("total", 0)),
        passed=int(totals.get("passed", 0)),
        failed=int(totals.get("failed", 0)),
        skipped=int(totals.get("skipped", 0)),
        reds=reds,
        advisory=advisory,
        expected=expected,
        expected_sha=expected_sha,
        actual_sha=str(payload.get("head_sha", "")),
        identities=identities,
        verdict_counts=verdict_counts,
        rows=len(scenarios),
        outcome_counts=counts,
    )
