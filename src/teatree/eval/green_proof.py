"""Verify one row per catalog scenario and version at the evaluated commit.

The ``eval-ci-heal`` combine job uses this pure gate after merging shard
summaries. A passing proof needs the exact catalog identity set, a matching SHA,
consistent totals and no gating red. Retry recoveries are FLAKY, never PASS.
Interactive behavioral reds remain visible but advisory; missing grading and
infrastructure failures always block.

:func:`evaluate_baseline` reuses the proof to answer the standing question a one-shard
smoke run cannot: is a recent full-suite proof over the CURRENT catalog on record at all.
"""

import dataclasses
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from teatree.eval.api_errors import is_usage_limit_skip_reason

#: The triage class a row carries when the producer wrote none — gating by construction.
UNCLASSIFIED = "unclassified"

#: How old the newest full-suite proof may be before the baseline reads STALE.
BASELINE_MAX_AGE = timedelta(days=7)


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
    incomplete: int
    reds: tuple[RedScenario, ...]
    advisory: tuple[RedScenario, ...] = ()
    unmeasured: tuple[RedScenario, ...] = ()
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
            and self.total == self.passed + self.failed + self.skipped + self.incomplete
            and self.passed == self.verdict_counts.get("pass", 0)
            and self.failed == self.verdict_counts.get("fail", 0)
            and self.skipped == self.verdict_counts.get("skip", 0)
            and self.incomplete == self.verdict_counts.get("incomplete", 0)
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
        return self.covers_the_catalog and self.incomplete == 0 and not self.reds and not self.unmeasured

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
        lines.extend(f"  UNMEASURED {row.name} [{row.lane}] -> the usage limit skipped it" for row in self.unmeasured)
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
            f"({self.passed}/{self.total} passed, {self.failed} failed, "
            f"{self.skipped} skipped, {self.incomplete} incomplete)"
        )


type _Partition = tuple[tuple[RedScenario, ...], tuple[RedScenario, ...], tuple[RedScenario, ...]]


def _partition(scenarios: Sequence[Any]) -> _Partition:
    """Split the non-null-``triage_class`` rows into (gating reds, advisory reds, unmeasured rows).

    An interactive BEHAVIOR_FAIL remains advisory because its verdict depends on
    the bundled Claude CLI's question rendering. INFRA_BLOCKED and UNVERIFIED
    always gate, including for interactive scenarios. A missing advisory flag
    also gates.

    A row with no ``triage_class`` KEY at all is likewise GATING: the producer
    always writes the key (``null`` for a pass), so its absence means the row was
    not produced by a grader this gate understands — the same fail-closed
    asymmetry ``advisory`` applies.

    A row the usage limit skipped never ran: it is unmeasured, which withholds the
    proof without being a red to fix.
    """
    reds: list[RedScenario] = []
    advisory: list[RedScenario] = []
    unmeasured: list[RedScenario] = []
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            reds.append(RedScenario(name="", lane="", triage_class=UNCLASSIFIED))
            continue
        outcome = scenario.get("outcome")
        verdict = scenario.get("verdict")
        if _is_unclassified(scenario):
            reds.append(
                RedScenario(name=_str(scenario, "name"), lane=_str(scenario, "lane"), triage_class=UNCLASSIFIED)
            )
            continue
        if verdict == "incomplete":
            continue
        if outcome == "FLAKY":
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
        if is_usage_limit_skip_reason(_str(scenario, "terminal_reason")):
            unmeasured.append(row)
            continue
        (advisory if bool(scenario.get("advisory")) and outcome == "BEHAVIOR_FAIL" else reds).append(row)
    return tuple(reds), tuple(advisory), tuple(unmeasured)


def _is_unclassified(scenario: Mapping[str, Any]) -> bool:
    if "triage_class" not in scenario:
        return True
    outcome = scenario.get("outcome")
    verdict = scenario.get("verdict")
    if outcome not in {"PASS", "FLAKY", "BEHAVIOR_FAIL", "INFRA_BLOCKED", "UNVERIFIED"}:
        return True
    if (outcome == "PASS" and verdict != "pass") or (outcome == "FLAKY" and verdict != "fail"):
        return True
    if verdict == "incomplete" and outcome != "UNVERIFIED":
        return True
    return outcome == "FLAKY" and scenario["triage_class"] is None


def _str(scenario: Mapping[str, Any], key: str) -> str:
    return str(scenario.get(key, ""))


def evaluate_green_proof(payload: Mapping[str, Any], *, expected: Mapping[str, str], expected_sha: str) -> GreenProof:
    """Check a merged summary against the selected catalog at *expected_sha*."""
    totals = payload.get("totals")
    totals = totals if isinstance(totals, Mapping) else {}
    scenarios = payload.get("scenarios")
    scenarios = scenarios if isinstance(scenarios, list) else []
    reds, advisory, unmeasured = _partition(scenarios)
    identities = tuple((_str(row, "name"), _str(row, "version")) for row in scenarios if isinstance(row, Mapping))
    counts = Counter(_str(row, "outcome") for row in scenarios if isinstance(row, Mapping))
    verdict_counts = Counter(_str(row, "verdict") for row in scenarios if isinstance(row, Mapping))
    return GreenProof(
        total=int(totals.get("total", 0)),
        passed=int(totals.get("passed", 0)),
        failed=int(totals.get("failed", 0)),
        skipped=int(totals.get("skipped", 0)),
        incomplete=int(totals.get("incomplete", 0)),
        reds=reds,
        advisory=advisory,
        unmeasured=unmeasured,
        expected=expected,
        expected_sha=expected_sha,
        actual_sha=str(payload.get("head_sha", "")),
        identities=identities,
        verdict_counts=verdict_counts,
        rows=len(scenarios),
        outcome_counts=counts,
    )


class BaselineState(StrEnum):
    MISSING = "missing"
    STALE = "stale"
    NOT_GREEN = "not_green"
    GREEN = "green"


@dataclasses.dataclass(frozen=True)
class BaselineStatus:
    """Whether a recent full-catalog green proof is on record, and why not when it is not."""

    state: BaselineState
    detail: str

    @property
    def is_green(self) -> bool:
        return self.state is BaselineState.GREEN

    @property
    def summary(self) -> str:
        return f"BASELINE {self.state.name}: {self.detail}"


def evaluate_baseline(
    payloads: Sequence[Mapping[str, Any]],
    *,
    expected: Mapping[str, str],
    now: datetime,
    max_age: timedelta = BASELINE_MAX_AGE,
) -> BaselineStatus:
    """Judge the newest measured merged summary (*payloads*, newest first) against the CURRENT catalog.

    A parked or empty summary measured nothing and is skipped. The first measured one must
    cover *expected* exactly (:attr:`GreenProof.covers_the_catalog`, bound to its own SHA),
    be no older than *max_age*, and carry no gating red; the verdict names the first check it fails.
    """
    payload = next((p for p in payloads if _measured_something(p)), None)
    if payload is None:
        return BaselineStatus(
            BaselineState.MISSING,
            "no full-suite eval-heal run is on record; dispatch eval-ci-heal.yml with no scenarios on main",
        )
    sha = str(payload.get("head_sha", ""))
    proof = evaluate_green_proof(payload, expected=expected, expected_sha=sha)
    if not proof.covers_the_catalog:
        return BaselineStatus(
            BaselineState.STALE,
            f"the newest proof ({sha[:9]}) measured {proof.total} of {len(expected)} current scenario(s), "
            "so it does not cover the catalog as it stands now",
        )
    generated_at = _instant(payload.get("generated_at"))
    if generated_at is None:
        return BaselineStatus(BaselineState.STALE, f"the newest proof ({sha[:9]}) carries no readable generated_at")
    age = now - generated_at
    if age > max_age:
        return BaselineStatus(
            BaselineState.STALE,
            f"the newest full-suite proof ({sha[:9]}) is {age.days} day(s) old, limit {max_age.days}",
        )
    if not proof.is_green:
        return BaselineStatus(
            BaselineState.NOT_GREEN,
            f"the newest proof ({sha[:9]}) has {len(proof.reds)} gating red(s), "
            f"{proof.incomplete} incomplete, {len(proof.unmeasured)} unmeasured",
        )
    return BaselineStatus(
        BaselineState.GREEN, f"{proof.passed}/{proof.total} passed at {sha[:9]}, {age.days} day(s) old"
    )


def _measured_something(payload: Mapping[str, Any]) -> bool:
    scenarios = payload.get("scenarios")
    return payload.get("parked") is not True and isinstance(scenarios, list) and bool(scenarios)


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
