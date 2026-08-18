"""The forge-read external-outcome measure (#4506).

The three read states are pinned apart, because collapsing any two of them is the
defect this measure exists to prevent: a completed read is a number, a box with no
forge is UNMEASURED, and a read that FAILED raises — it never degrades into the
zero-merges answer that downstream reads as "the factory shipped nothing".
"""

import datetime as dt
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.external_outcomes import (
    DEFAULT_EXTERNAL_WINDOW_DAYS,
    EXTERNAL_OUTCOME_TTL,
    ExternalOutcomeReadError,
    ExternalOutcomeStatus,
    Forge,
    read_external_outcomes,
    refresh_if_stale,
    resolve_forge,
)
from teatree.core.models import ExternalOutcomeSnapshot
from teatree.types import RawAPIDict

_SLUGS = ("acme/app",)


class _StubHost:
    """A code host whose merged-PR read is scripted, and which counts its calls."""

    def __init__(self, *, hits: list[RawAPIDict] | None = None, raises: Exception | None = None) -> None:
        self._hits = hits or []
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    def list_merged_prs_since(self, *, repo: str, since: str) -> list[RawAPIDict]:
        self.calls.append((repo, since))
        if self._raises is not None:
            raise self._raises
        return self._hits


def _hit(number: int, *, url: str = "") -> RawAPIDict:
    return {"number": number, "html_url": url or f"https://example.test/pr/{number}"}


class ReadExternalOutcomesTestCase(TestCase):
    def test_completed_read_counts_the_forge_merges(self) -> None:
        host = _StubHost(hits=[_hit(11), _hit(12)])

        outcomes = read_external_outcomes(Forge(host=host, repo_slugs=_SLUGS))

        assert outcomes.status is ExternalOutcomeStatus.OK
        assert outcomes.merged_pr_count == 2
        assert [ref.number for ref in outcomes.merged_prs] == [11, 12]
        assert [ref.slug for ref in outcomes.merged_prs] == ["acme/app", "acme/app"]

    def test_window_start_is_passed_to_the_forge_as_the_since_date(self) -> None:
        host = _StubHost()
        now = dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.UTC)

        read_external_outcomes(Forge(host=host, repo_slugs=_SLUGS), window_days=7, now=now)

        assert host.calls == [("acme/app", "2026-08-11")]

    def test_gitlab_shaped_hit_is_read_through_iid(self) -> None:
        host = _StubHost(hits=[{"iid": 42, "web_url": "https://gl.test/mr/42"}])

        outcomes = read_external_outcomes(Forge(host=host, repo_slugs=_SLUGS))

        assert [ref.number for ref in outcomes.merged_prs] == [42]
        assert outcomes.merged_prs[0].url == "https://gl.test/mr/42"

    def test_hit_with_no_usable_number_is_dropped_not_guessed(self) -> None:
        host = _StubHost(hits=[{"title": "no number here"}, _hit(7)])

        outcomes = read_external_outcomes(Forge(host=host, repo_slugs=_SLUGS))

        assert [ref.number for ref in outcomes.merged_prs] == [7]

    def test_a_non_numeric_number_string_is_dropped_not_guessed(self) -> None:
        host = _StubHost(hits=[{"number": "not-a-number"}, _hit(7)])

        outcomes = read_external_outcomes(Forge(host=host, repo_slugs=_SLUGS))

        assert [ref.number for ref in outcomes.merged_prs] == [7]

    def test_a_non_mapping_hit_is_dropped_not_guessed(self) -> None:
        host = _StubHost(hits=["not-a-dict", _hit(7)])

        outcomes = read_external_outcomes(Forge(host=host, repo_slugs=_SLUGS))

        assert [ref.number for ref in outcomes.merged_prs] == [7]

    def test_no_host_is_unmeasured_never_zero_merges(self) -> None:
        outcomes = read_external_outcomes(Forge(host=None, repo_slugs=_SLUGS))

        assert outcomes.status is ExternalOutcomeStatus.NO_FORGE
        assert outcomes.status is not ExternalOutcomeStatus.OK

    def test_no_declared_repos_is_unmeasured_never_zero_merges(self) -> None:
        outcomes = read_external_outcomes(Forge(host=_StubHost(), repo_slugs=()))

        assert outcomes.status is ExternalOutcomeStatus.NO_FORGE

    def test_failed_read_raises_rather_than_reporting_zero_merges(self) -> None:
        host = _StubHost(raises=RuntimeError("HTTP 403 rate limited"))

        with pytest.raises(ExternalOutcomeReadError) as caught:
            read_external_outcomes(Forge(host=host, repo_slugs=_SLUGS))

        assert "acme/app" in str(caught.value)
        assert "rate limited" in str(caught.value)


class ResolveForgeTestCase(TestCase):
    """The un-injected default path: derive the host + repo scope from the overlay."""

    def test_resolves_the_overlays_own_host_and_declared_repos(self) -> None:
        overlay = object()
        host = _StubHost()
        with (
            patch("teatree.core.factory.external_outcomes.get_overlay", return_value=overlay) as get_overlay,
            patch("teatree.core.factory.external_outcomes.code_host_from_overlay", return_value=host) as code_host,
            patch("teatree.core.factory.external_outcomes.owned_repo_slugs", return_value=_SLUGS) as slugs,
        ):
            forge = resolve_forge(overlay="mine")

        assert forge == Forge(host=host, repo_slugs=_SLUGS)
        code_host.assert_called_once_with("mine")
        get_overlay.assert_called_once_with("mine")
        slugs.assert_called_once_with(overlay)


class RefreshIfStaleTestCase(TestCase):
    def test_first_read_records_a_snapshot(self) -> None:
        host = _StubHost(hits=[_hit(1), _hit(2), _hit(3)])

        snapshot = refresh_if_stale(forge=Forge(host=host, repo_slugs=_SLUGS))

        assert snapshot.merged_pr_count == 3
        assert snapshot.status == ExternalOutcomeStatus.OK.value
        assert ExternalOutcomeSnapshot.objects.count() == 1

    def test_a_fresh_snapshot_is_served_without_touching_the_network(self) -> None:
        host = _StubHost(hits=[_hit(1)])
        now = timezone.now()
        refresh_if_stale(forge=Forge(host=host, repo_slugs=_SLUGS), now=now)
        assert len(host.calls) == 1

        served = refresh_if_stale(forge=Forge(host=host, repo_slugs=_SLUGS), now=now + dt.timedelta(minutes=5))

        assert len(host.calls) == 1
        assert served.merged_pr_count == 1
        assert ExternalOutcomeSnapshot.objects.count() == 1

    def test_a_snapshot_past_its_ttl_triggers_a_fresh_read(self) -> None:
        host = _StubHost(hits=[_hit(1)])
        now = timezone.now()
        refresh_if_stale(forge=Forge(host=host, repo_slugs=_SLUGS), now=now)

        refresh_if_stale(
            forge=Forge(host=host, repo_slugs=_SLUGS), now=now + EXTERNAL_OUTCOME_TTL + dt.timedelta(minutes=1)
        )

        assert len(host.calls) == 2
        assert ExternalOutcomeSnapshot.objects.count() == 2

    def test_a_failed_read_records_no_snapshot(self) -> None:
        host = _StubHost(raises=RuntimeError("boom"))

        with pytest.raises(ExternalOutcomeReadError):
            refresh_if_stale(forge=Forge(host=host, repo_slugs=_SLUGS))

        assert ExternalOutcomeSnapshot.objects.count() == 0

    def test_another_overlays_snapshot_does_not_satisfy_this_overlays_ttl(self) -> None:
        host = _StubHost(hits=[_hit(1)])
        now = timezone.now()
        refresh_if_stale(forge=Forge(host=host, repo_slugs=_SLUGS), overlay="other", now=now)

        refresh_if_stale(forge=Forge(host=host, repo_slugs=_SLUGS), overlay="mine", now=now)

        assert len(host.calls) == 2


class ExternalOutcomeSnapshotModelTestCase(TestCase):
    def test_str_names_the_status_count_and_window(self) -> None:
        snapshot = ExternalOutcomeSnapshot.objects.record(
            read_external_outcomes(Forge(host=_StubHost(hits=[_hit(1)]), repo_slugs=_SLUGS)),
        )

        text = str(snapshot)

        assert "ok" in text
        assert "merged=1" in text
        assert f"window={DEFAULT_EXTERNAL_WINDOW_DAYS}d" in text
