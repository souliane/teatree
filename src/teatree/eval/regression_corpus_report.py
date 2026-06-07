"""Rendering for the deterministic regression corpus report.

Separated from :mod:`teatree.eval.regression_corpus` (which runs the checks) so
each module owns one concern: running vs. presenting. The CLI imports both
renderers from the corpus module, which re-exports them.
"""

import json

from teatree.eval.regression_corpus_models import RegressionReport


def render_text(report: RegressionReport) -> str:
    lines: list[str] = []
    for r in report.results:
        status = "SKIP" if r.skipped else ("PASS" if r.ok else "FAIL")
        line = f"{status} {r.check.failure_class}"
        if r.detail:
            line += f" — {r.detail}"
        lines.append(line)
    passed = sum(1 for r in report.results if r.ok and not r.skipped)
    skipped = sum(1 for r in report.results if r.skipped)
    lines.append(f"\nsummary: {passed} passed, {len(report.failures)} failed, {skipped} skipped")
    return "\n".join(lines)


def render_json(report: RegressionReport) -> str:
    return json.dumps(
        {
            "ok": report.ok,
            "checks": [
                {
                    "failure_class": r.check.failure_class,
                    "origin": r.check.origin,
                    "ok": r.ok,
                    "skipped": r.skipped,
                    "detail": r.detail,
                }
                for r in report.results
            ],
        },
        indent=2,
    )
