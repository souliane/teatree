"""Parity: the PR-sweep CI gate and the §17.4 keystone classify through ONE function.

#12 was a sibling-classifier divergence — the sweep hardcoded ``test (3.13)`` and
blocked on non-required advisory checks with no newest-per-name dedupe, while the
keystone read the branch-protection required set and deduped (the #2583 fix landed
keystone-side only). Both now route the core green/pending/failed verdict through
:func:`teatree.core.merge.classify_required_rollup`.

This feeds a fixture matrix through BOTH surfaces — the keystone's live
``fetch_required_checks_status`` (real ``gh`` classification, subprocess stubbed)
AND the sweep's ``_ci_gate`` — and asserts each agrees with the shared function,
so a future edit that reintroduces a divergent sweep classifier fails here. Only
the unstoppable ``gh`` subprocess and the required-set lookup are stubbed; the
classification under test is real on both sides.
"""

# test-path: cross-cutting — a keystone (core.merge) x PR-sweep (loop.scanners)
# contract test that pins the two surfaces to the ONE shared classifier; it
# spans both packages by design and mirrors no single src module.

import json
from collections.abc import Callable
from unittest.mock import patch

import pytest

from teatree.core.merge import classify_required_rollup, fetch_required_checks_status
from teatree.loop.scanners.pr_sweep import PrSummary, PrSweepScanner
from teatree.loop.scanners.pr_sweep_adapters import NullMergeNotifier
from teatree.types import RawAPIDict

# No DB access — pure classification through stubbed forge I/O on both surfaces.

_SLUG = "souliane/teatree"
_PR = 4242
_T0 = "2026-06-19T10:00:00Z"
_T1 = "2026-06-19T10:05:00Z"
_T2 = "2026-06-19T11:00:00Z"
_T3 = "2026-06-19T11:05:00Z"


def _check(name: str, *, conclusion: str = "SUCCESS", status: str = "COMPLETED", newer: bool = False) -> RawAPIDict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "startedAt": _T2 if newer else _T0,
        "completedAt": _T3 if newer else _T1,
    }


# (label, rollup, required_names, expected shared verdict). No uv-audit-only-red
# case: that is a sweep-specific fallback layered ON TOP of the shared verdict, so
# it is excluded here — this matrix pins the CORE verdict the two share.
_MATRIX: list[tuple[str, list[RawAPIDict], set[str], str]] = [
    ("all required green", [_check("test (3.13)"), _check("lint")], {"test (3.13)", "lint"}, "green"),
    (
        "required failed",
        [_check("test (3.13)", conclusion="FAILURE"), _check("lint")],
        {"test (3.13)", "lint"},
        "failed",
    ),
    (
        "required pending",
        [_check("test (3.13)", status="IN_PROGRESS", conclusion=""), _check("lint")],
        {"test (3.13)", "lint"},
        "pending",
    ),
    ("required missing from rollup", [_check("test (3.13)")], {"test (3.13)", "lint"}, "pending"),
    (
        "non-required failed is ignored",
        [_check("test (3.13)"), _check("eval", conclusion="FAILURE")],
        {"test (3.13)"},
        "green",
    ),
    (
        "stale failure superseded by newer success (dedupe)",
        [_check("test (3.13)", conclusion="FAILURE"), _check("test (3.13)", newer=True)],
        {"test (3.13)"},
        "green",
    ),
    ("no required gate configured", [_check("eval", conclusion="FAILURE")], set(), "green"),
]


def _gh_stub(rollup: list[RawAPIDict], required: set[str]) -> Callable[[list[str]], tuple[int, str, str]]:
    def run(argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "statusCheckRollup" in joined:
            return (0, json.dumps(rollup), "")
        if "baseRefName" in joined:
            return (0, "main", "")
        if "required_status_checks" in joined:
            return (0, json.dumps({"contexts": sorted(required)}), "")
        return (0, "", "")

    return run


def _keystone_verdict(rollup: list[RawAPIDict], required: set[str]) -> str:
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub(rollup, required)):
        return fetch_required_checks_status(_SLUG, _PR, host_kind="github")


def _sweep_verdict(rollup: list[RawAPIDict], required: set[str]) -> str:
    scanner = PrSweepScanner(repos=(), api=object(), keystone=object(), notifier=NullMergeNotifier())
    pr = PrSummary(
        slug=_SLUG,
        number=_PR,
        head_sha="a" * 40,
        is_draft=False,
        has_changes_requested=False,
        rollup=tuple(rollup),
    )
    with patch("teatree.loop.scanners.pr_sweep.fetch_required_context_names", return_value=required):
        skip_reason, fallback, _failing = scanner._ci_gate(pr)
    if skip_reason is None and not fallback:
        return "green"
    if skip_reason == "ci_pending":
        return "pending"
    if skip_reason == "ci_red":
        return "failed"
    msg = f"unexpected sweep gate result: {skip_reason!r}, fallback={fallback}"
    raise AssertionError(msg)


@pytest.mark.parametrize(("label", "rollup", "required", "expected"), _MATRIX, ids=[m[0] for m in _MATRIX])
def test_keystone_and_sweep_agree_via_shared_classifier(
    label: str,
    rollup: list[RawAPIDict],
    required: set[str],
    expected: str,
) -> None:
    shared = classify_required_rollup(rollup, required)
    assert shared == expected, f"{label}: shared classifier {shared!r} != expected {expected!r}"
    assert _keystone_verdict(rollup, required) == shared, f"{label}: keystone diverged from the shared classifier"
    assert _sweep_verdict(rollup, required) == shared, f"{label}: sweep gate diverged from the shared classifier"
