"""Exclude vendored code from coverage analysis (issue #1873).

``coverage json`` reports every file present in a repo's ``.coverage`` data,
which on a repo whose data was collected without ``[run] omit`` exclusions
includes vendored dependencies under ``.venv/``, ``site-packages/`` and
``node_modules/``. Counting those into the total skews the metric — a repo
with high source coverage looks under-covered (or vice versa) because the
percentage is diluted by untracked third-party code.

This module filters a ``coverage json`` ``files`` map down to first-party
files and recomputes the percentage from the surviving statements.
"""

from typing import NotRequired, TypedDict

VENDORED_PATTERNS: tuple[str, ...] = (".venv", "site-packages", "node_modules")


class CoverageResult(TypedDict):
    """First-party coverage outcome: a percent, or unavailable.

    ``percent`` is present only when ``available`` is ``True`` — when no
    first-party statements remain the metric is genuinely unavailable.
    """

    available: bool
    percent: NotRequired[float]


def is_vendored_path(path: str) -> bool:
    """True when any path segment is a vendored directory.

    Matches on whole path segments only, so a real source file whose name
    merely *contains* a pattern (``src/myvenv_helper.py``) is not excluded.
    """
    segments = path.replace("\\", "/").split("/")
    return any(segment in VENDORED_PATTERNS for segment in segments)


def recompute_percent(files: dict[str, dict]) -> CoverageResult:
    """Recompute coverage percent over first-party files only.

    ``files`` is the ``coverage json`` top-level ``files`` map: each value
    carries a ``summary`` with ``covered_lines`` and ``num_statements``.
    Vendored entries are dropped before summing. When no first-party
    statements remain the metric is genuinely unavailable.
    """
    covered = 0
    statements = 0
    for path, entry in files.items():
        if is_vendored_path(path):
            continue
        summary = entry.get("summary", {})
        covered += summary.get("covered_lines", 0)
        statements += summary.get("num_statements", 0)
    if statements == 0:
        return {"available": False}
    return {"available": True, "percent": round(covered / statements * 100, 1)}
