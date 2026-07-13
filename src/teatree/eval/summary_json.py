"""The publish-safe per-scenario ``--summary-json`` artifact (§2.4).

The one machine-readable eval artifact carrying a triage class: a per-scenario
record of ``name`` / ``lane`` / ``verdict`` plus the triage discriminators
(``is_error`` / ``terminal_reason`` / ``matcher_failed`` / ``judge_failed``) and
the ``triage_class`` :func:`teatree.eval.triage.classify_red` derives from them.
It is built ONLY from spec identity + verdict + discriminators — NEVER a
transcript, a tool-call input, or a judge rationale — the same sanitization
contract as :func:`teatree.eval.report.render_summary_markdown`, so the CI heal
workflow can upload it as a published artifact. Works for a single-trial run and
for a ``--trials``/pass@k run (the aggregate discriminators fold over each
scenario's per-trial results).
"""

import dataclasses
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from teatree.eval.api_errors import THROTTLE_TERMINAL_PREFIX
from teatree.eval.discovery import find_spec
from teatree.eval.pass_at_k import PassAtKResult
from teatree.eval.report import ScenarioResult
from teatree.eval.triage import ScenarioRecord, ScenarioTriage, classify_red

_HEAD_SHA_ENV_VAR = "GITHUB_SHA"

AnyResult = ScenarioResult | PassAtKResult


@dataclasses.dataclass(frozen=True)
class _ScenarioRow:
    """One publish-safe per-scenario record — identity, verdict, and discriminators.

    Never carries ``run.text_blocks`` / ``run.tool_calls`` / a tool-call ``input``
    / a ``judge.rationale``, so the serialized JSON is safe to publish.
    """

    name: str
    lane: str
    verdict: str
    is_error: bool
    terminal_reason: str
    matcher_failed: bool
    judge_failed: bool

    def as_json(self) -> ScenarioRecord:
        triage = classify_red(
            ScenarioTriage(
                verdict=self.verdict,
                is_error=self.is_error,
                terminal_reason=self.terminal_reason,
                matcher_failed=self.matcher_failed,
                judge_failed=self.judge_failed,
            )
        )
        return {
            "name": self.name,
            "lane": self.lane,
            "verdict": self.verdict,
            "is_error": self.is_error,
            "terminal_reason": self.terminal_reason,
            "matcher_failed": self.matcher_failed,
            "judge_failed": self.judge_failed,
            "triage_class": triage.value if triage is not None else None,
        }


def _judge_failed(result: ScenarioResult) -> bool:
    return result.judge is not None and not result.judge.skipped and not result.judge.passed


def _row_from_scenario(result: ScenarioResult) -> _ScenarioRow:
    return _ScenarioRow(
        name=result.spec.name,
        lane=result.spec.lane,
        verdict=result.verdict,
        is_error=result.run.is_error,
        terminal_reason=result.run.terminal_reason,
        matcher_failed=any(not m.passed for m in result.matcher_results),
        judge_failed=_judge_failed(result),
    )


def _pass_at_k_terminal_reason(result: PassAtKResult, executed: Sequence[ScenarioResult]) -> str:
    """The aggregate terminal reason: a cap outranks a throttle common to every trial."""
    if result.terminal_reason:
        return result.terminal_reason
    if executed and all(t.run.terminal_reason.startswith(THROTTLE_TERMINAL_PREFIX) for t in executed):
        return executed[0].run.terminal_reason
    return ""


def _row_from_pass_at_k(result: PassAtKResult) -> _ScenarioRow:
    verdict = "skip" if result.skipped else ("pass" if result.ok else "fail")
    executed = [t for t in result.trial_results if not t.skipped]
    spec = find_spec(result.spec_name)
    return _ScenarioRow(
        name=result.spec_name,
        lane=spec.lane if spec is not None else "unknown",
        verdict=verdict,
        # An errored aggregate needs EVERY executed trial to have errored — one
        # clean trial with a real matcher diff is behavioral signal, not transport.
        is_error=bool(executed) and all(t.run.is_error for t in executed),
        terminal_reason=_pass_at_k_terminal_reason(result, executed),
        matcher_failed=any(any(not m.passed for m in t.matcher_results) for t in executed),
        judge_failed=any(_judge_failed(t) for t in executed),
    )


def _row(result: AnyResult) -> _ScenarioRow:
    return _row_from_scenario(result) if isinstance(result, ScenarioResult) else _row_from_pass_at_k(result)


def _model_of(results: Sequence[AnyResult]) -> str:
    for result in results:
        if isinstance(result, ScenarioResult):
            return result.spec.model
        spec = find_spec(result.spec_name)
        if spec is not None:
            return spec.model
    return "unknown"


def render_summary_json(results: Sequence[AnyResult], *, head_sha: str, generated_at: str) -> str:
    """Render the publish-safe per-scenario JSON (§2.4); ``head_sha``/``generated_at`` are injected.

    Injecting the sha and timestamp keeps the function pure and deterministic —
    the CLI writer resolves them from the environment and clock. Accepts either
    the single-trial ``list[ScenarioResult]`` or the multi-trial
    ``Sequence[PassAtKResult]``.
    """
    rows = [_row(result) for result in results]
    totals = {
        "total": len(rows),
        "passed": sum(1 for r in rows if r.verdict == "pass"),
        "failed": sum(1 for r in rows if r.verdict == "fail"),
        "skipped": sum(1 for r in rows if r.verdict == "skip"),
    }
    payload = {
        "generated_at": generated_at,
        "model": _model_of(results),
        "head_sha": head_sha,
        "totals": totals,
        "scenarios": [row.as_json() for row in rows],
    }
    return json.dumps(payload, indent=2)


def write_summary_json(results: Sequence[AnyResult], path: Path) -> None:
    """Resolve ``head_sha`` (``GITHUB_SHA`` env) + ``generated_at`` (clock) and write the JSON.

    The single writer both the single-trial and pass@k lanes call, so the
    environment/clock resolution lives in one place and the pure renderer stays
    testable with explicit values.
    """
    path.write_text(
        render_summary_json(
            results,
            head_sha=os.environ.get(_HEAD_SHA_ENV_VAR, ""),
            generated_at=datetime.now(UTC).isoformat(),
        ),
        encoding="utf-8",
    )
