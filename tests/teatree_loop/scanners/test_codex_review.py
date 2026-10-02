"""Truncation-visibility + per-PR fault isolation for ``codex_review`` (F5.4, F5.6).

* **F5.4** — ``GhCodexPrApi.list_open_self_prs`` passes an explicit high
    ``--limit`` (the ``gh`` default of 30 would silently drop the overflow) and
    warns when the returned page fills to the cap, so a genuine >200-PR repo is
    visible rather than silently truncated.
* **F5.6** — a single PR whose classification raises must not drop the codex
    dispatch for the other PRs in the sweep.
"""

import json
import logging
import subprocess
from dataclasses import dataclass, field, replace
from unittest.mock import patch

import pytest

from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.codex_review import (
    _LIST_OPEN_PRS_LIMIT,
    CodexReviewScanner,
    GhCodexPrApi,
    PrSummary,
    head_checks_unsettled,
)
from teatree.types import ScannerError

_SLUG = "souliane/teatree"
_PROBE = "teatree.loop.scanners.codex_review.head_checks_unsettled"
_QUERY = "teatree.loop.scanners.codex_review.CodeHostQuery"
# codex_review binds ``run_allowed_to_fail`` at module import, so patch the name
# in the scanner's namespace (not the utils source).
_RUN = "teatree.loop.scanners.codex_review.run_allowed_to_fail"


def _completed(*, returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


def _pr_json(number: int) -> dict[str, object]:
    return {
        "number": number,
        "headRefOid": f"{number:040x}",
        "isDraft": False,
        "url": f"https://github.com/{_SLUG}/pull/{number}",
        "title": f"PR {number}",
        "author": {"login": "souliane"},
        "files": [],
    }


class TestListLimit:
    """F5.4: an explicit high limit is requested and a filled page is warned about."""

    def test_argv_carries_high_explicit_limit(self) -> None:
        api = GhCodexPrApi()
        with patch(_RUN, return_value=_completed(returncode=0, stdout="[]")) as run:
            api.list_open_self_prs(slug=_SLUG)
        argv = run.call_args.args[0]
        assert "--limit" in argv
        assert str(_LIST_OPEN_PRS_LIMIT) in argv
        assert _LIST_OPEN_PRS_LIMIT >= 200

    def test_full_page_warns_about_truncation(self, caplog) -> None:
        payload = json.dumps([_pr_json(n) for n in range(_LIST_OPEN_PRS_LIMIT)])
        api = GhCodexPrApi()
        with (
            patch(_RUN, return_value=_completed(returncode=0, stdout=payload)),
            patch(_PROBE, return_value=False),
            caplog.at_level(logging.WARNING, logger="teatree.loop.scanners.codex_review"),
        ):
            prs = api.list_open_self_prs(slug=_SLUG)
        assert len(prs) == _LIST_OPEN_PRS_LIMIT
        assert any("cap" in rec.message for rec in caplog.records)

    def test_under_limit_does_not_warn(self, caplog) -> None:
        payload = json.dumps([_pr_json(1), _pr_json(2)])
        api = GhCodexPrApi()
        with (
            patch(_RUN, return_value=_completed(returncode=0, stdout=payload)),
            patch(_PROBE, return_value=False),
            caplog.at_level(logging.WARNING, logger="teatree.loop.scanners.codex_review"),
        ):
            prs = api.list_open_self_prs(slug=_SLUG)
        assert len(prs) == 2
        assert not caplog.records

    def test_empty_route_never_inherits_ambient_gh(self, monkeypatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "ambient-token")
        empty = ForgeTokenResolution("github_token", "owner", ForgeTokenState.UNSET, detail="route unset")
        with (
            patch("teatree.forge_credentials.resolve_slug_token", return_value=empty),
            patch(_RUN) as run,
            pytest.raises(ScannerError, match="refusing ambient gh authentication"),
        ):
            GhCodexPrApi().list_open_self_prs(slug=_SLUG)
        run.assert_not_called()


@dataclass
class _FaultyApi:
    """Lists a fixed set of PRs; ``_evaluate`` raises for the poisoned number."""

    prs: list[PrSummary]

    def list_open_self_prs(self, *, slug: str) -> list[PrSummary]:
        _ = slug
        return list(self.prs)


def _summary(number: int) -> PrSummary:
    return PrSummary(
        slug=_SLUG,
        number=number,
        head_sha=f"{number:040x}",
        is_draft=False,
        changed_files=(),
        url=f"https://github.com/{_SLUG}/pull/{number}",
    )


class TestPerPrIsolation:
    """F5.6: one PR's evaluation error does not drop the others."""

    def test_one_bad_pr_does_not_drop_the_rest(self) -> None:
        api = _FaultyApi(prs=[_summary(1), _summary(2), _summary(3)])
        scanner = CodexReviewScanner(repos=(_SLUG,), api=api)

        real_evaluate = CodexReviewScanner._evaluate

        def _evaluate(self: CodexReviewScanner, pr: PrSummary) -> ScanSignal | None:
            if pr.number == 2:
                msg = "visibility probe failed"
                raise RuntimeError(msg)
            return real_evaluate(self, pr)

        with patch.object(CodexReviewScanner, "_evaluate", _evaluate):
            signals = scanner.scan()

        dispatched = sorted(s.payload["pr_id"] for s in signals if s.kind == "codex_review.dispatch")
        assert dispatched == [1, 3]


@dataclass
class _RecordingApi:
    seen: list[str] = field(default_factory=list)
    prs: list[PrSummary] = field(default_factory=list)

    def list_open_self_prs(self, *, slug: str) -> list[PrSummary]:
        self.seen.append(slug)
        return list(self.prs)


class TestHealthyScan:
    def test_all_prs_dispatch_normally(self) -> None:
        api = _RecordingApi(prs=[_summary(1), _summary(2)])
        scanner = CodexReviewScanner(repos=(_SLUG,), api=api)
        signals = scanner.scan()
        assert sorted(s.payload["pr_id"] for s in signals) == [1, 2]


class TestHeadChecksUnsettled:
    """The predicate that decides whether a head has settled enough to be worth a review."""

    def test_pending_required_checks_are_unsettled(self) -> None:
        with patch(_QUERY) as query:
            query.for_ref.return_value.required_checks_status.return_value = "pending"
            assert head_checks_unsettled(slug=_SLUG, pr_id=1254) is True

    def test_failing_required_checks_are_unsettled(self) -> None:
        with patch(_QUERY) as query:
            query.for_ref.return_value.required_checks_status.return_value = "failed"
            assert head_checks_unsettled(slug=_SLUG, pr_id=1254) is True

    def test_green_required_checks_are_settled(self) -> None:
        with patch(_QUERY) as query:
            query.for_ref.return_value.required_checks_status.return_value = "green"
            assert head_checks_unsettled(slug=_SLUG, pr_id=1254) is False

    def test_unreadable_rollup_never_skips_the_review(self) -> None:
        # Missing evidence spends a run rather than silently skipping one: this gate
        # only ever DELAYS a review, so a forge outage must not mute the doublecheck.
        with patch(_QUERY) as query:
            query.for_ref.return_value.required_checks_status.return_value = "unreadable"
            assert head_checks_unsettled(slug=_SLUG, pr_id=1254) is False

    def test_a_raising_probe_never_skips_the_review(self) -> None:
        with patch(_QUERY) as query:
            query.for_ref.side_effect = RuntimeError("no backend provider")
            assert head_checks_unsettled(slug=_SLUG, pr_id=1254) is False


class TestAdapterStampsHeadChecks:
    """``list_open_self_prs`` resolves the standing ONCE so neither scanner re-probes."""

    def test_decoded_pr_carries_the_probe_verdict(self) -> None:
        payload = json.dumps([_pr_json(7)])
        with (
            patch(_RUN, return_value=_completed(returncode=0, stdout=payload)),
            patch(_PROBE, return_value=True) as probe,
        ):
            prs = GhCodexPrApi().list_open_self_prs(slug=_SLUG)

        assert [pr.checks_unsettled for pr in prs] == [True]
        assert probe.call_args.kwargs == {"slug": _SLUG, "pr_id": 7}

    def test_draft_pr_is_never_probed(self) -> None:
        # Nothing reviews a draft, so its required-checks read would be pure cost.
        payload = json.dumps([{**_pr_json(8), "isDraft": True}])
        with (
            patch(_RUN, return_value=_completed(returncode=0, stdout=payload)),
            patch(_PROBE, return_value=True) as probe,
        ):
            prs = GhCodexPrApi().list_open_self_prs(slug=_SLUG)

        assert [pr.checks_unsettled for pr in prs] == [False]
        assert probe.call_count == 0


class TestUnsettledHeadIsNotReviewed:
    def test_unsettled_head_dispatches_no_codex_review(self) -> None:
        unsettled = replace(_summary(1), checks_unsettled=True)
        scanner = CodexReviewScanner(repos=(_SLUG,), api=_RecordingApi(prs=[unsettled]))

        assert scanner.scan() == []

    def test_control_settled_head_still_dispatches(self) -> None:
        scanner = CodexReviewScanner(repos=(_SLUG,), api=_RecordingApi(prs=[_summary(1)]))

        assert [s.payload["pr_id"] for s in scanner.scan()] == [1]
