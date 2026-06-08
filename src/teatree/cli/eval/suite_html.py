"""Whole-suite HTML report for a ``t3 eval`` run (#280).

The terminal `t3 eval` table is ephemeral; CI needs a human-readable artifact.
This renders the run's lane outcomes (the same :class:`~teatree.cli.eval.all.LaneResult`
data the terminal table is built from) to a self-contained HTML file — inline
CSS, no external assets — with a plain-language final verdict at the top so a
non-expert can read "is this good?" at a glance, then the lane table
(lane | verdict | cost | duration | detail).

Every run-derived value is HTML-escaped so a lane detail string (which can carry
a transcript fragment) can never inject markup.

``build_suite_verdict`` is the plain-language verdict. It mirrors PR #2182's
``cli/eval/verdict.build_verdict`` shape (three honest outcomes) but is computed
from the on-main ``LaneResult`` (which has no ``setup_hint`` yet). When #2182
merges, the two collapse to one helper.
"""

from collections.abc import Sequence
from html import escape

from teatree.cli.eval.all import LaneResult

_STYLE = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 system-ui, sans-serif; margin: 2rem; max-width: 60rem; }
h1 { font-size: 1.4rem; }
.verdict { font-size: 1.05rem; font-weight: 600; margin: 0 0 1.25rem; padding: 0.6rem 0.9rem; }
.verdict { border-radius: 6px; border-left: 4px solid #6e7781; background: rgba(127,127,127,0.08); }
.verdict.good { border-left-color: #1a7f37; }
.verdict.problems { border-left-color: #cf222e; }
.verdict.noted { border-left-color: #9a6700; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #d0d7de; }
th { font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.status { font-weight: 600; }
.status.pass { color: #1a7f37; }
.status.fail { color: #cf222e; }
.status.skip { color: #6e7781; }
""".strip()


def build_suite_verdict(lanes: Sequence[LaneResult]) -> str:
    """Plain-language closing verdict, keyed off the lane outcomes.

    Three honest shapes: a real FAIL names the failing lane(s); else a
    skipped-but-everything-else-green run notes the skip was NOT validated; else
    everything that ran passed.
    """
    failed = [lane for lane in lanes if not lane.passed and not lane.skipped]
    if failed:
        names = ", ".join(lane.name for lane in failed)
        plural = "checks" if len(failed) > 1 else "check"
        return f"❌ PROBLEMS FOUND — {len(failed)} {plural} failed ({names}); see the row(s) below."
    skipped = [lane for lane in lanes if lane.skipped]
    ran = len(lanes) - len(skipped)
    if skipped:
        names = ", ".join(lane.name for lane in skipped)
        return f"✅ {ran} lane(s) passed. ⚠️ {names}: SKIPPED — not run, not yet validated."
    return f"✅ ALL GOOD — every check passed ({len(lanes)} lanes)."


def _verdict_class(lanes: Sequence[LaneResult]) -> str:
    if any(not lane.passed and not lane.skipped for lane in lanes):
        return "problems"
    if any(lane.skipped for lane in lanes):
        return "noted"
    return "good"


def _status_class(lane: LaneResult) -> str:
    if lane.skipped:
        return "skip"
    return "pass" if lane.passed else "fail"


def _row(lane: LaneResult) -> str:
    cls = _status_class(lane)
    return (
        "<tr>"
        f"<td>{escape(lane.name)}</td>"
        f'<td class="status {cls}">{lane.status}</td>'
        f"<td>{escape(lane.cost)}</td>"
        f'<td class="num">{lane.duration_s:.1f}s</td>'
        f"<td>{escape(lane.detail)}</td>"
        "</tr>"
    )


def render_suite_html(lanes: Sequence[LaneResult]) -> str:
    """Render the whole-suite run as a self-contained HTML report string."""
    verdict = build_suite_verdict(lanes)
    rows = "\n".join(_row(lane) for lane in lanes)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Eval suite report</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n<body>\n<h1>Eval suite report</h1>\n"
        f'<p class="verdict {_verdict_class(lanes)}">{escape(verdict)}</p>\n'
        "<table>\n<thead><tr>"
        "<th>Lane</th><th>Verdict</th><th>Cost</th><th>Duration</th><th>Detail</th>"
        "</tr></thead>\n<tbody>\n"
        f"{rows}\n"
        "</tbody>\n</table>\n</body>\n</html>\n"
    )
