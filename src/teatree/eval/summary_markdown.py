"""The SANITIZED aggregate markdown dashboard for a single- or multi-trial run.

Sibling of :mod:`teatree.eval.summary_json`: both build a publish-safe aggregate
from the spec identity and the verdict alone, never from a transcript. The full
per-run renderers (text / JSON / HTML), which DO read transcripts, stay in
:mod:`teatree.eval.report`.
"""

import dataclasses
from collections.abc import Sequence
from typing import TYPE_CHECKING

from teatree.eval.discovery import find_spec
from teatree.eval.report import ScenarioResult

if TYPE_CHECKING:
    from teatree.eval.pass_at_k import PassAtKResult


@dataclasses.dataclass(frozen=True)
class _SummaryRow:
    """One sanitized per-scenario row — name, lane, verdict, and pass/trial count.

    Built ONLY from the spec identity (``name``/``lane``/``model``) and the
    aggregate verdict + pass/trial counts. It never touches ``run.text_blocks``,
    ``run.tool_calls``, a tool-call ``input``, or a ``judge.rationale``, so the
    rendered markdown is publish-safe — the transcript stays in the private
    ``--transcript-html`` artifact.
    """

    scenario: str
    lane: str
    verdict: str
    trials: str
    cost: str
    #: A red the CAP produced with every matcher already satisfied. Rendered as a
    #: ``(cap)`` suffix on the verdict CELL while ``verdict`` stays the plain
    #: ``fail`` the counts are derived from — so the published dashboard routes this
    #: class to the harness without downloading a per-trial artifact, and without
    #: the row escaping the failure count.
    cap_truncated: bool = False


def _lane_of(spec_name: str, lane: str | None) -> str:
    if lane is not None:
        return lane
    spec = find_spec(spec_name)
    return spec.lane if spec is not None else "unknown"


def _verdict_cell(row: _SummaryRow) -> str:
    return f"{row.verdict} (cap)" if row.cap_truncated else row.verdict


def _summary_counts_for_rows(rows: Sequence[_SummaryRow]) -> tuple[int, int, int]:
    passed = sum(1 for r in rows if r.verdict == "pass")
    failed = sum(1 for r in rows if r.verdict == "fail")
    skipped = sum(1 for r in rows if r.verdict == "skip")
    return passed, failed, skipped


def _row_from_scenario(result: ScenarioResult) -> _SummaryRow:
    return _SummaryRow(
        scenario=result.spec.name,
        lane=result.spec.lane,
        verdict=result.verdict,
        trials="-" if result.skipped else "1/1",
        cost="-" if result.skipped else f"${result.run.cost_usd:.4f}",
        cap_truncated=result.cap_truncated_matchers_satisfied,
    )


def _row_from_pass_at_k(result: "PassAtKResult") -> _SummaryRow:
    verdict = "skip" if result.skipped else ("pass" if result.ok else "fail")
    reds = [t for t in result.trial_results if not t.passed]
    return _SummaryRow(
        scenario=result.spec_name,
        lane=_lane_of(result.spec_name, None),
        verdict=verdict,
        trials="-" if result.skipped else f"{result.passes}/{result.trials}",
        cost="-" if result.skipped else f"${result.cost_usd:.4f}",
        cap_truncated=verdict == "fail" and bool(reds) and all(t.cap_truncated_matchers_satisfied for t in reds),
    )


def _model_of(results: Sequence[ScenarioResult] | Sequence["PassAtKResult"]) -> str:
    for item in results:
        if isinstance(item, ScenarioResult):
            return item.spec.model
        spec = find_spec(item.spec_name)
        if spec is not None:
            return spec.model
    return "unknown"


def render_summary_markdown(results: Sequence[ScenarioResult] | Sequence["PassAtKResult"]) -> str:
    """Render a SANITIZED aggregate markdown dashboard for a single- or multi-trial run.

    A header (overall pass/fail/skip counts, total metered cost, model) plus a
    ``scenario | lane | verdict | trials | cost`` table — the per-scenario metered
    cost makes an expensive scenario obvious at a glance. Accepts either the single-trial
    ``list[ScenarioResult]`` or the multi-trial ``Sequence[PassAtKResult]``;
    ``trials`` is ``1/1`` for a single trial and ``passes/trials`` (e.g. ``2/3``)
    for pass@k. Built ONLY from the spec identity, the verdict, and pass/trial
    counts — it NEVER reads a transcript (``text_blocks``/``tool_calls``/tool-call
    ``input``) or a judge rationale, so it is safe to publish to a PR's
    ``$GITHUB_STEP_SUMMARY`` and the weekly public dashboard. The private
    transcript is the separate ``--transcript-html`` artifact.
    """
    rows = [
        _row_from_scenario(item) if isinstance(item, ScenarioResult) else _row_from_pass_at_k(item) for item in results
    ]
    passed, failed, skipped = _summary_counts_for_rows(rows)
    total_cost_usd = sum(item.run.cost_usd if isinstance(item, ScenarioResult) else item.cost_usd for item in results)
    model = _model_of(results)
    header = (
        f"**{passed} passed**, **{failed} failed**, **{skipped} skipped** (of {len(rows)}) "
        f"· model `{model}` · cost ${total_cost_usd:.4f}"
    )
    table = [
        "| scenario | lane | verdict | trials | cost |",
        "| --- | --- | --- | --- | --- |",
        *(f"| {row.scenario} | {row.lane} | {_verdict_cell(row)} | {row.trials} | {row.cost} |" for row in rows),
    ]
    return "\n".join([header, "", *table, ""])
