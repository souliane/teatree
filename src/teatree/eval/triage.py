"""The pure CI-eval red triage classifier — the single source of ``triage_class``.

A red behavioral-eval scenario is either a *behavioral* regression (the model did
the wrong thing — must be fixed) or an *infra* failure (transport error, throttle,
or a resource cap — retried, never "fixed" by editing code). :func:`classify_red`
derives that class ONCE, from the exact discriminator fields the eval already
produces (``verdict`` / ``is_error`` / ``terminal_reason`` / ``matcher_failed`` /
``judge_failed``), so the ``--summary-json`` producer and ``t3 eval ci-status``
never re-derive it divergently. The cap set and throttle prefix are imported from
their canonical homes (:data:`teatree.eval.models.CAP_TERMINAL_REASONS`,
:data:`teatree.eval.api_errors.THROTTLE_TERMINAL_PREFIX`) rather than copied.
"""

import dataclasses
from enum import StrEnum
from typing import TypedDict

from teatree.eval.api_errors import THROTTLE_TERMINAL_PREFIX
from teatree.eval.models import CAP_TERMINAL_REASONS


class ScenarioRecord(TypedDict, total=False):
    """The publish-safe per-scenario JSON shape (§2.4) — the wire contract both sides share.

    The ``--summary-json`` producer writes every key; a consumer reading the
    downloaded artifact may see a partial record, so it is ``total=False`` and read
    defensively. It carries only spec identity + verdict + the triage
    discriminators + the derived ``triage_class`` — never a transcript.
    """

    name: str
    lane: str
    verdict: str
    is_error: bool
    terminal_reason: str
    matcher_failed: bool
    judge_failed: bool
    triage_class: str | None


class TriageClass(StrEnum):
    """The six mutually-exclusive classes a red (or skipped) scenario falls into.

    ``BEHAVIORAL`` and ``JUDGE`` demand a code fix; the three ``INFRA_*`` classes
    are retried (never fixed); ``NO_COVERAGE`` (a skip under ``--require-executed``)
    is a wiring bug that must never masquerade as green.
    """

    BEHAVIORAL = "behavioral"
    INFRA_TRANSPORT = "infra_transport"
    INFRA_THROTTLE = "infra_throttle"
    INFRA_CAP = "infra_cap"
    JUDGE = "judge"
    NO_COVERAGE = "no_coverage"


@dataclasses.dataclass(frozen=True)
class ScenarioTriage:
    """The publish-safe discriminators :func:`classify_red` reads — never a transcript.

    Built by the ``--summary-json`` producer from a graded result, or parsed back
    from the JSON artifact by ``ci-status`` via :meth:`from_json`, so both sides
    classify off the identical field set.
    """

    verdict: str
    is_error: bool
    terminal_reason: str
    matcher_failed: bool
    judge_failed: bool

    @classmethod
    def from_json(cls, scenario: ScenarioRecord) -> "ScenarioTriage":
        return cls(
            verdict=str(scenario.get("verdict", "")),
            is_error=bool(scenario.get("is_error")),
            terminal_reason=str(scenario.get("terminal_reason", "")),
            matcher_failed=bool(scenario.get("matcher_failed")),
            judge_failed=bool(scenario.get("judge_failed")),
        )


def classify_red(scenario: ScenarioTriage) -> TriageClass | None:
    """The §3.5 triage table; ``None`` for a passing scenario, else the fail class.

    A ``skip`` is a coverage bug regardless of any other field; a passing scenario
    is not red. Every other verdict is a failure, classified by
    :func:`_classify_fail`.
    """
    if scenario.verdict == "pass":
        return None
    if scenario.verdict == "skip":
        return TriageClass.NO_COVERAGE
    return _classify_fail(scenario)


def _classify_fail(scenario: ScenarioTriage) -> TriageClass:
    """Classify a FAILED scenario by first-match precedence.

    An errored run is transport infra even if a matcher also failed; a throttle or
    a resource cap outrank a matcher diff (a cap-truncated trajectory is not
    trustworthy behavioral signal); a judge-only red (every matcher passed) is
    ``JUDGE``; anything else that failed is ``BEHAVIORAL`` — the safe default that
    routes an unclassified red to a code fix, never a silent retry.
    """
    if scenario.is_error:
        return TriageClass.INFRA_TRANSPORT
    if scenario.terminal_reason.startswith(THROTTLE_TERMINAL_PREFIX):
        return TriageClass.INFRA_THROTTLE
    if scenario.terminal_reason in CAP_TERMINAL_REASONS:
        return TriageClass.INFRA_CAP
    if scenario.judge_failed and not scenario.matcher_failed:
        return TriageClass.JUDGE
    return TriageClass.BEHAVIORAL
