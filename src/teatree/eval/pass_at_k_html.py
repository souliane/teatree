"""Per-trial transcript HTML report for a metered pass@k ``t3 eval run``.

The whole-suite summary (:mod:`teatree.cli.eval.suite_html`) renders one row per
LANE — it answers "which lane is red". It cannot answer "WHY did this scenario
fail", because the per-trial trajectories never reach it. This report fills that
gap: for the metered ``--trials k`` run that CI executes, it renders, per
scenario, the aggregate verdict plus EACH trial's transcript — the agent's
reasoning (``run.text_blocks``), its tool calls (``run.tool_calls``), and the
failing matchers — so a maintainer can open the uploaded artifact and diagnose a
red lane (e.g. the under_load drift scenarios) without re-running anything.

It is a sibling of :func:`teatree.eval.report.render_html` (which renders a
single-trial ``list[ScenarioResult]``); this one renders the multi-trial
``list[PassAtKResult]``, drilling into each result's ``trial_results``. Every
run-derived value is HTML-escaped so a transcript fragment can never inject
markup.
"""

import json
from collections.abc import Sequence
from html import escape
from itertools import starmap

from teatree.eval.models import EvalRun, EvalToolCall
from teatree.eval.pass_at_k import PassAtKResult
from teatree.eval.report import ScenarioResult

_STYLE = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 system-ui, sans-serif; margin: 2rem; max-width: 70rem; }
h1 { font-size: 1.4rem; }
.summary { margin: 0 0 1.5rem; font-weight: 600; }
.summary .pass { color: #1a7f37; }
.summary .fail { color: #cf222e; }
.summary .skip { color: #6e7781; }
details { border: 1px solid #d0d7de; border-radius: 6px; margin: 0.5rem 0; padding: 0.5rem 0.75rem; }
details.pass { border-left: 4px solid #1a7f37; }
details.fail { border-left: 4px solid #cf222e; }
details.skip { border-left: 4px solid #6e7781; }
summary { cursor: pointer; font-weight: 600; }
.verdict { font-size: 0.8rem; padding: 0.1rem 0.45rem; border-radius: 999px; margin-right: 0.5rem; color: #fff; }
.verdict.pass { background: #1a7f37; }
.verdict.fail { background: #cf222e; }
.verdict.skip { background: #6e7781; }
.reason { color: #6e7781; font-weight: 400; }
.trial { margin: 0.75rem 0 0; padding-left: 0.75rem; border-left: 2px solid #d0d7de; }
.trial h3 { font-size: 0.95rem; margin: 0 0 0.35rem; }
.label { color: #6e7781; font-weight: 600; text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.03em; }
ul.matchers { margin: 0.35rem 0 0; }
pre { white-space: pre-wrap; word-break: break-word; background: rgba(127,127,127,0.1); padding: 0.5rem; }
pre { border-radius: 4px; margin: 0.25rem 0; }
""".strip()


def _aggregate_verdict(result: PassAtKResult) -> str:
    if result.skipped:
        return "skip"
    return "pass" if result.ok else "fail"


def _summary(results: Sequence[PassAtKResult]) -> str:
    total = len(results)
    skipped = sum(1 for r in results if r.skipped)
    passed = sum(1 for r in results if not r.skipped and r.ok)
    failed = total - passed - skipped
    return (
        '<p class="summary">'
        f'<span class="pass">{passed} passed</span>, '
        f'<span class="fail">{failed} failed</span>, '
        f'<span class="skip">{skipped} skipped</span> '
        f"(of {total} scenarios, pass@{results[0].trials if results else 0})</p>"
    )


def _tool_call(call: EvalToolCall) -> str:
    rendered = json.dumps(call.input, indent=2, sort_keys=True, default=str)
    return f"<pre>turn {call.turn}: {escape(call.name)}({escape(rendered)})</pre>"


def _transcript(run: EvalRun) -> str:
    parts: list[str] = []
    if run.text_blocks:
        joined = "\n\n".join(run.text_blocks)
        parts.append(f'<p class="label">reasoning / final answer</p>\n<pre>{escape(joined)}</pre>')
    if run.tool_calls:
        calls = "\n".join(_tool_call(call) for call in run.tool_calls)
        parts.append(f'<p class="label">tool calls</p>\n{calls}')
    if not parts:
        parts.append('<p class="reason">(no transcript captured — the trial produced no text or tool calls)</p>')
    return "\n".join(parts)


def _trial_block(index: int, result: ScenarioResult) -> str:
    verdict = result.verdict
    reason = escape(result.run.terminal_reason)
    body_parts = [_transcript(result.run)]
    failed_matchers = [m for m in result.matcher_results if not m.passed]
    if failed_matchers:
        items = "\n".join(f"<li><pre>{escape(m.message)}</pre></li>" for m in failed_matchers)
        body_parts.append(f'<p class="label">failed matchers</p>\n<ul class="matchers">\n{items}\n</ul>')
    if result.judge is not None and not result.judge.skipped:
        judge_verdict = "pass" if result.judge.passed else "fail"
        body_parts.append(f'<p class="label">judge ({judge_verdict})</p>\n<pre>{escape(result.judge.rationale)}</pre>')
    body = "\n".join(body_parts)
    return (
        f'<div class="trial">\n'
        f'<h3><span class="verdict {verdict}">{verdict.upper()}</span>trial {index} '
        f'<span class="reason">({reason})</span></h3>\n'
        f"{body}\n</div>"
    )


def _scenario_block(result: PassAtKResult) -> str:
    verdict = _aggregate_verdict(result)
    head = (
        f'<summary><span class="verdict {verdict}">{verdict.upper()}</span>'
        f"{escape(result.spec_name)} "
        f'<span class="reason">({result.passes}/{result.trials} trials passed)</span></summary>'
    )
    trials = "\n".join(starmap(_trial_block, enumerate(result.trial_results, start=1)))
    if not result.trial_results:
        trials = '<p class="reason">(scenario skipped — no trials executed)</p>'
    return f'<details class="{verdict}">\n{head}\n{trials}\n</details>'


def render_pass_at_k_html(results: Sequence[PassAtKResult]) -> str:
    """Render a metered pass@k run as a self-contained per-trial transcript report.

    For each scenario: the aggregate verdict, then one block per trial showing
    that trial's PASS/FAIL, the agent's reasoning + tool calls (the transcript),
    and any failed matchers / judge rationale. Self-contained (inline CSS, no
    external assets); every run-derived value is HTML-escaped.
    """
    summary = _summary(results)
    rows = "\n".join(_scenario_block(result) for result in results)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Eval transcripts (pass@k)</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n<body>\n<h1>Eval transcripts — per-trial evidence</h1>\n"
        f"{summary}\n{rows}\n</body>\n</html>\n"
    )
