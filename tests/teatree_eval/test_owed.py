import datetime as dt
import io
import json
import subprocess
import zipfile
from collections.abc import Sequence

import pytest

from teatree.eval.owed import (
    LEDGER_ARTIFACT,
    LEDGER_FILENAME,
    MAX_LISTING_PAGES,
    PAGE_SIZE,
    PR_OWED_ARTIFACT,
    PR_OWED_FILENAME,
    OwedHarvest,
    OwedLedger,
    OwedLedgerError,
    OwedReason,
    OwedScenario,
    TruncatedListingError,
)

REPO = "souliane/teatree"
NOW = dt.datetime(2026, 10, 16, 3, 0, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)


def _owed(name: str, *, reason: OwedReason = OwedReason.DEFERRED, since: dt.datetime = NOW) -> OwedScenario:
    return OwedScenario(name=name, reason=reason, source="pr#1@abc", since=since)


def _ledger(*entries: OwedScenario, harvested_through: dt.datetime | None = None) -> OwedLedger:
    return OwedLedger(scenarios=entries, harvested_through=harvested_through)


class TestRecorded:
    def test_adds_stripped_names_and_ignores_blanks(self) -> None:
        ledger = OwedLedger().recorded(
            [" alpha ", "", "   ", "beta", "alpha"], reason=OwedReason.PARKED, source="pr#7@abc", since=NOW
        )
        assert ledger.names == ("alpha", "beta")
        assert {entry.reason for entry in ledger.scenarios} == {OwedReason.PARKED}

    def test_an_already_owed_name_keeps_its_older_debt(self) -> None:
        older = _ledger(_owed("alpha", since=NOW - DAY))
        ledger = older.recorded(["alpha"], reason=OwedReason.LIMIT, source="nightly@9", since=NOW)
        assert ledger.scenarios == older.scenarios


class TestMerged:
    def test_a_duplicate_name_keeps_the_oldest_since(self) -> None:
        newer = _ledger(_owed("alpha", reason=OwedReason.DEFERRED, since=NOW))
        older = _ledger(_owed("alpha", reason=OwedReason.LIMIT, since=NOW - DAY))
        assert newer.merged(older).scenarios == older.scenarios
        assert older.merged(newer).scenarios == older.scenarios

    def test_union_by_name_and_latest_harvest_mark(self) -> None:
        left = _ledger(_owed("alpha"), harvested_through=NOW - DAY)
        right = _ledger(_owed("beta"), harvested_through=NOW)
        merged = left.merged(right)
        assert merged.names == ("alpha", "beta")
        assert merged.harvested_through == NOW

    def test_no_harvest_mark_on_either_side_stays_none(self) -> None:
        assert _ledger(_owed("alpha")).merged(_ledger(_owed("beta"))).harvested_through is None


class TestSettled:
    def test_clears_passed_keeps_failed_and_drops_catalog_unknown(self) -> None:
        ledger = _ledger(_owed("passed_one"), _owed("failed_one"), _owed("zeta_gone"), _owed("alpha_gone"))
        settled, dropped = ledger.settled(["passed_one"], known={"passed_one", "failed_one"})
        assert settled.names == ("failed_one",)
        assert dropped == ("alpha_gone", "zeta_gone")

    def test_keeps_the_harvest_mark(self) -> None:
        settled, _ = _ledger(_owed("alpha"), harvested_through=NOW).settled(["alpha"], known={"alpha"})
        assert settled == OwedLedger(harvested_through=NOW)

    def test_a_confirmed_red_moves_behind_newer_debt_after_it_was_attempted(self) -> None:
        ledger = _ledger(_owed("old_red", since=NOW - 2 * DAY), _owed("new_park", since=NOW - DAY))
        settled, _ = ledger.settled([], known={"old_red", "new_park"}, attempted=["old_red"], now=NOW)
        assert settled.next_batch(1, {"old_red", "new_park"}) == ("new_park",)
        assert OwedLedger.from_json(settled.to_json()).next_batch(1, {"old_red", "new_park"}) == ("new_park",)


class TestNextBatch:
    def test_oldest_first_then_name_capped_and_catalog_known_only(self) -> None:
        ledger = _ledger(
            _owed("newest", since=NOW),
            _owed("b_old", since=NOW - DAY),
            _owed("a_old", since=NOW - DAY),
            _owed("unknown_oldest", since=NOW - 2 * DAY),
        )
        known = {"newest", "a_old", "b_old"}
        assert ledger.next_batch(2, known) == ("a_old", "b_old")
        assert ledger.next_batch(10, known) == ("a_old", "b_old", "newest")

    @pytest.mark.parametrize("cap", [0, -1])
    def test_a_cap_below_one_raises(self, cap: int) -> None:
        with pytest.raises(ValueError, match="cap"):
            _ledger(_owed("alpha")).next_batch(cap, {"alpha"})

    def test_older_repeated_reds_do_not_starve_new_debt(self) -> None:
        reds = tuple(_owed(f"red_{index}", since=NOW - 2 * DAY) for index in range(14))
        ledger = _ledger(*reds, _owed("new_park", since=NOW - DAY))
        settled, _ = ledger.settled([], known=ledger.names, attempted=[row.name for row in reds], now=NOW)
        assert "new_park" in settled.next_batch(14, ledger.names)


class TestIsParked:
    @pytest.mark.parametrize(
        ("reasons", "parked"),
        [
            ((OwedReason.PARKED,), True),
            ((OwedReason.LIMIT,), True),
            ((OwedReason.DEFERRED, OwedReason.LIMIT), True),
            ((OwedReason.DEFERRED,), False),
            ((), False),
        ],
    )
    def test_parked_or_limit_parks_and_deferred_alone_does_not(
        self, reasons: tuple[OwedReason, ...], *, parked: bool
    ) -> None:
        ledger = _ledger(*(_owed(f"s{index}", reason=reason) for index, reason in enumerate(reasons)))
        assert ledger.is_parked is parked


_ROW: dict[str, object] = {
    "name": "alpha",
    "reason": "parked",
    "source": "pr#1@abc",
    "since": "2026-10-16T03:00:00+00:00",
}


def _ledger_json(*rows: dict[str, object], harvested_through: str | None = None) -> str:
    return json.dumps({"version": 1, "harvested_through": harvested_through, "scenarios": list(rows) or [_ROW]})


class TestJson:
    def test_round_trip_is_lossless_and_stable(self) -> None:
        ledger = _ledger(
            _owed("beta", reason=OwedReason.LIMIT, since=NOW),
            _owed("alpha", reason=OwedReason.PARKED, since=NOW - DAY),
            harvested_through=NOW,
        )
        text = ledger.to_json()
        assert OwedLedger.from_json(text) == ledger.merged(OwedLedger())
        assert OwedLedger.from_json(text).to_json() == text
        payload = json.loads(text)
        assert payload["version"] == 1
        assert [row["name"] for row in payload["scenarios"]] == ["alpha", "beta"]

    def test_an_offset_datetime_normalises_to_utc(self) -> None:
        text = _ledger_json({**_ROW, "since": "2026-10-16T05:00:00+02:00"})
        assert OwedLedger.from_json(text).scenarios[0].since == NOW

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("{not json", id="bad-json"),
            pytest.param("[]", id="not-an-object"),
            pytest.param(json.dumps({"harvested_through": None, "scenarios": []}), id="missing-version"),
            pytest.param(json.dumps({"version": 2, "harvested_through": None, "scenarios": []}), id="wrong-version"),
            pytest.param(json.dumps({"version": True, "harvested_through": None, "scenarios": []}), id="bool-version"),
            pytest.param(json.dumps({"version": 1, "scenarios": []}), id="missing-harvest-mark"),
            pytest.param(json.dumps({"version": 1, "harvested_through": None, "scenarios": {}}), id="scenarios-object"),
            pytest.param(_ledger_json({**_ROW, "reason": "later"}), id="unknown-reason"),
            pytest.param(_ledger_json({**_ROW, "since": "2026-10-16T03:00:00"}), id="naive-since"),
            pytest.param(_ledger_json({**_ROW, "since": "yesterday"}), id="unparsable-since"),
            pytest.param(_ledger_json({**_ROW, "name": ""}), id="blank-name"),
            pytest.param(_ledger_json({**_ROW, "name": 7}), id="non-string-name"),
            pytest.param(
                _ledger_json({key: value for key, value in _ROW.items() if key != "source"}), id="missing-source"
            ),
            pytest.param(_ledger_json(harvested_through="2026-10-16T03:00:00"), id="naive-harvest-mark"),
            pytest.param(_ledger_json(_ROW, _ROW), id="duplicate-name"),
        ],
    )
    def test_malformed_input_raises_never_degrades_to_empty(self, text: str) -> None:
        with pytest.raises(OwedLedgerError):
            OwedLedger.from_json(text)


class FakeGh:
    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, args: Sequence[str]) -> bytes:
        assert args[0] == "api", args
        endpoint = args[1]
        self.calls.append(endpoint)
        if endpoint not in self.routes:
            raise subprocess.CalledProcessError(1, ["gh", *args], stderr=b"HTTP 404")
        return self.routes[endpoint]


def _zip(member: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, text)
    return buffer.getvalue()


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _artifact(
    artifact_id: int,
    *,
    created: dt.datetime,
    head_sha: str = "f" * 40,
    expired: bool = False,
    provenance: tuple[str, int] = ("main", 1),
) -> dict:
    return {
        "id": artifact_id,
        "expired": expired,
        "created_at": _iso(created),
        "workflow_run": {
            "id": artifact_id * 10,
            "head_sha": head_sha,
            "head_branch": provenance[0],
            "head_repository_id": provenance[1],
        },
    }


def _listing(artifacts: list[dict]) -> bytes:
    return json.dumps({"total_count": len(artifacts), "artifacts": artifacts}).encode()


def _artifacts_url(name: str, page: int = 1) -> str:
    return f"repos/{REPO}/actions/artifacts?name={name}&per_page={PAGE_SIZE}&page={page}"


def _pulls_url(page: int = 1) -> str:
    return f"repos/{REPO}/pulls?state=closed&sort=updated&direction=desc&per_page={PAGE_SIZE}&page={page}"


def _zip_url(artifact_id: int) -> str:
    return f"repos/{REPO}/actions/artifacts/{artifact_id}/zip"


def _pull(number: int, head_sha: str, *, merged: dt.datetime | None, updated: dt.datetime | None = None) -> dict:
    return {
        "number": number,
        "merged_at": None if merged is None else _iso(merged),
        "updated_at": _iso(updated or merged or NOW),
        "head": {"sha": head_sha},
    }


def _harvest(routes: dict[str, bytes]) -> tuple[OwedHarvest, FakeGh, list[str]]:
    gh = FakeGh({f"repos/{REPO}": b'{"id":1,"default_branch":"main"}', **routes})
    warnings: list[str] = []
    return OwedHarvest(gh=gh, repo=REPO, warn=warnings.append), gh, warnings


class TestPreviousLedger:
    def test_foreign_or_nondefault_artifacts_cannot_replace_the_ledger(self) -> None:
        trusted = _ledger(_owed("trusted"), harvested_through=NOW - DAY)
        routes = {
            _artifacts_url(LEDGER_ARTIFACT): _listing(
                [
                    _artifact(3, created=NOW, provenance=("main", 2)),
                    _artifact(2, created=NOW - dt.timedelta(hours=1), provenance=("feature", 1)),
                    _artifact(1, created=NOW - DAY),
                ]
            ),
            _zip_url(1): _zip(LEDGER_FILENAME, trusted.to_json()),
        }
        harvest, gh, _ = _harvest(routes)
        assert harvest.previous_ledger() == trusted
        assert _zip_url(2) not in gh.calls
        assert _zip_url(3) not in gh.calls

    def test_the_newest_non_expired_ledger_is_read(self) -> None:
        newest = _ledger(_owed("from_newest"), harvested_through=NOW - DAY)
        routes = {
            _artifacts_url(LEDGER_ARTIFACT): _listing(
                [
                    _artifact(3, created=NOW, expired=True),
                    _artifact(2, created=NOW - DAY),
                    _artifact(1, created=NOW - 2 * DAY),
                ]
            ),
            _zip_url(2): _zip(LEDGER_FILENAME, newest.to_json()),
        }
        harvest, gh, _ = _harvest(routes)
        assert harvest.previous_ledger() == newest
        assert _zip_url(1) not in gh.calls

    def test_a_full_page_of_expired_ledgers_reads_the_next_page(self) -> None:
        live = _ledger(_owed("from_page_two"), harvested_through=NOW - DAY)
        expired = [_artifact(1000 + index, created=NOW, expired=True) for index in range(PAGE_SIZE)]
        routes = {
            _artifacts_url(LEDGER_ARTIFACT, 1): _listing(expired),
            _artifacts_url(LEDGER_ARTIFACT, 2): _listing([_artifact(2, created=NOW - 2 * DAY)]),
            _zip_url(2): _zip(LEDGER_FILENAME, live.to_json()),
        }
        harvest, _, _ = _harvest(routes)
        assert harvest.previous_ledger() == live

    def test_no_live_ledger_artifact_is_none(self) -> None:
        routes = {_artifacts_url(LEDGER_ARTIFACT): _listing([_artifact(1, created=NOW, expired=True)])}
        harvest, _, _ = _harvest(routes)
        assert harvest.previous_ledger() is None

    def test_a_zip_without_the_ledger_member_fails_loud(self) -> None:
        routes = {
            _artifacts_url(LEDGER_ARTIFACT): _listing([_artifact(1, created=NOW)]),
            _zip_url(1): _zip("something-else.json", "{}"),
        }
        harvest, _, _ = _harvest(routes)
        with pytest.raises(OwedLedgerError, match=LEDGER_FILENAME):
            harvest.previous_ledger()

    def test_a_non_zip_download_fails_loud(self) -> None:
        routes = {
            _artifacts_url(LEDGER_ARTIFACT): _listing([_artifact(1, created=NOW)]),
            _zip_url(1): b"not a zip",
        }
        harvest, _, _ = _harvest(routes)
        with pytest.raises(OwedLedgerError):
            harvest.previous_ledger()

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(b"<html>", id="not-json"),
            pytest.param(b"\xff\xfe", id="not-utf8"),
            pytest.param(b"[]", id="not-an-object"),
            pytest.param(json.dumps({"artifacts": [{"id": "1"}]}).encode(), id="string-id"),
            pytest.param(json.dumps({"artifacts": [{"id": 1, "expired": "no"}]}).encode(), id="string-flag"),
        ],
    )
    def test_an_unparsable_listing_fails_loud(self, body: bytes) -> None:
        harvest, _, _ = _harvest({_artifacts_url(LEDGER_ARTIFACT): body})
        with pytest.raises(OwedLedgerError):
            harvest.previous_ledger()

    def test_a_gh_failure_propagates(self) -> None:
        harvest, _, _ = _harvest({})
        with pytest.raises(subprocess.CalledProcessError):
            harvest.previous_ledger()


class TestMergedPrHeads:
    def test_only_prs_merged_since_the_mark_are_kept(self) -> None:
        routes = {
            _pulls_url(): json.dumps(
                [
                    _pull(10, "a" * 40, merged=NOW - dt.timedelta(hours=1)),
                    _pull(11, "b" * 40, merged=None, updated=NOW),
                    _pull(12, "c" * 40, merged=NOW - 2 * DAY, updated=NOW - dt.timedelta(hours=2)),
                    _pull(13, "d" * 40, merged=NOW - DAY),
                ]
            ).encode()
        }
        harvest, _, _ = _harvest(routes)
        assert harvest.merged_pr_heads(NOW - DAY) == [(10, "a" * 40), (13, "d" * 40)]

    def test_a_full_page_still_updated_after_the_mark_reads_the_next_page(self) -> None:
        recent = NOW - dt.timedelta(hours=1)
        first = [_pull(number, f"{number:040d}", merged=recent) for number in range(1, PAGE_SIZE + 1)]
        second = [_pull(999, "e" * 40, merged=recent), _pull(1000, "f" * 40, merged=NOW - 3 * DAY)]
        routes = {_pulls_url(1): json.dumps(first).encode(), _pulls_url(2): json.dumps(second).encode()}
        harvest, _, _ = _harvest(routes)
        heads = harvest.merged_pr_heads(NOW - DAY)
        assert len(heads) == PAGE_SIZE + 1
        assert heads[-1] == (999, "e" * 40)

    def test_a_page_ending_before_the_mark_stops_paging(self) -> None:
        stale = [_pull(number, "a" * 40, merged=None, updated=NOW - 3 * DAY) for number in range(PAGE_SIZE)]
        harvest, gh, _ = _harvest({_pulls_url(1): json.dumps(stale).encode()})
        assert harvest.merged_pr_heads(NOW - DAY) == []
        assert gh.calls == [_pulls_url(1)]

    def test_a_non_list_payload_fails_loud(self) -> None:
        harvest, _, _ = _harvest({_pulls_url(): b'{"message": "Bad credentials"}'})
        with pytest.raises(OwedLedgerError, match="JSON list"):
            harvest.merged_pr_heads(NOW - DAY)

    def test_more_pages_than_the_bound_fails_loud_never_truncates(self) -> None:
        recent = NOW - dt.timedelta(hours=1)
        page = json.dumps([_pull(1, "a" * 40, merged=recent)] * PAGE_SIZE).encode()
        routes = {_pulls_url(index): page for index in range(1, MAX_LISTING_PAGES + 1)}
        harvest, _, _ = _harvest(routes)
        with pytest.raises(OwedLedgerError, match="truncated"):
            harvest.merged_pr_heads(NOW - DAY)


class TestPrOwed:
    def test_foreign_head_repository_cannot_poison_pr_debt(self) -> None:
        head = "a" * 40
        trusted = _ledger(_owed("trusted"))
        routes = {
            _artifacts_url(PR_OWED_ARTIFACT): _listing(
                [
                    _artifact(2, created=NOW, head_sha=head, provenance=("main", 2)),
                    _artifact(1, created=NOW - DAY, head_sha=head, provenance=("feature", 1)),
                ]
            ),
            _zip_url(1): _zip(PR_OWED_FILENAME, trusted.to_json()),
        }
        harvest, gh, _ = _harvest(routes)
        assert harvest.pr_owed([(10, head)]) == trusted
        assert _zip_url(2) not in gh.calls

    def test_only_the_artifact_on_the_final_head_sha_is_harvested(self) -> None:
        final, older = "a" * 40, "0" * 40
        on_final = _ledger(_owed("owed_on_final", reason=OwedReason.LIMIT))
        on_older = _ledger(_owed("owed_on_older_push"))
        routes = {
            _artifacts_url(PR_OWED_ARTIFACT): _listing(
                [
                    _artifact(5, created=NOW - DAY, head_sha=final),
                    _artifact(4, created=NOW - 2 * DAY, head_sha=older),
                ]
            ),
            _zip_url(5): _zip(PR_OWED_FILENAME, on_final.to_json()),
            _zip_url(4): _zip(PR_OWED_FILENAME, on_older.to_json()),
        }
        harvest, gh, _ = _harvest(routes)
        assert harvest.pr_owed([(10, final)]).names == ("owed_on_final",)
        assert _zip_url(4) not in gh.calls

    def test_the_newest_artifact_wins_when_a_head_ran_twice(self) -> None:
        head = "a" * 40
        rerun = _ledger(_owed("from_rerun"))
        routes = {
            _artifacts_url(PR_OWED_ARTIFACT): _listing(
                [_artifact(6, created=NOW - 2 * DAY, head_sha=head), _artifact(7, created=NOW - DAY, head_sha=head)]
            ),
            _zip_url(7): _zip(PR_OWED_FILENAME, rerun.to_json()),
        }
        harvest, _, _ = _harvest(routes)
        assert harvest.pr_owed([(10, head)]).names == ("from_rerun",)

    def test_an_expired_artifact_warns_naming_the_pr(self) -> None:
        head = "a" * 40
        routes = {
            _artifacts_url(PR_OWED_ARTIFACT): _listing([_artifact(5, created=NOW - DAY, head_sha=head, expired=True)])
        }
        harvest, _, warnings = _harvest(routes)
        assert harvest.pr_owed([(42, head)]) == OwedLedger()
        assert len(warnings) == 1
        assert warnings[0].startswith("::warning::")
        assert "PR #42" in warnings[0]

    def test_a_pr_with_no_artifact_owes_nothing(self) -> None:
        routes = {_artifacts_url(PR_OWED_ARTIFACT): _listing([_artifact(5, created=NOW, head_sha="b" * 40)])}
        harvest, _, warnings = _harvest(routes)
        assert harvest.pr_owed([(42, "a" * 40)]) == OwedLedger()
        assert warnings == []

    def test_no_merged_pr_reads_nothing(self) -> None:
        harvest, gh, _ = _harvest({})
        assert harvest.pr_owed([]) == OwedLedger()
        assert gh.calls == []

    def test_paging_stops_once_every_head_is_found(self) -> None:
        head = "a" * 40
        full_page = [_artifact(1000 + index, created=NOW, head_sha="b" * 40) for index in range(PAGE_SIZE - 1)]
        routes = {
            _artifacts_url(PR_OWED_ARTIFACT, 1): _listing([*full_page, _artifact(5, created=NOW, head_sha=head)]),
            _zip_url(5): _zip(PR_OWED_FILENAME, _ledger(_owed("found")).to_json()),
        }
        harvest, gh, _ = _harvest(routes)
        assert harvest.pr_owed([(42, head)]).names == ("found",)
        assert _artifacts_url(PR_OWED_ARTIFACT, 2) not in gh.calls

    def test_an_unfound_head_past_the_page_bound_fails_loud_never_truncates(self) -> None:
        page = _listing([_artifact(1000 + index, created=NOW, head_sha="b" * 40) for index in range(PAGE_SIZE)])
        routes = {_artifacts_url(PR_OWED_ARTIFACT, index): page for index in range(1, MAX_LISTING_PAGES + 1)}
        harvest, _, _ = _harvest(routes)
        with pytest.raises(TruncatedListingError, match=PR_OWED_ARTIFACT):
            harvest.pr_owed([(42, "a" * 40)])

    def test_old_artifact_page_ends_the_relevant_harvest_window(self) -> None:
        old = NOW - dt.timedelta(days=100)
        page = _listing([_artifact(1000 + index, created=old, head_sha="b" * 40) for index in range(PAGE_SIZE)])
        harvest, gh, _ = _harvest({_artifacts_url(PR_OWED_ARTIFACT, 1): page})
        assert harvest.pr_owed([(42, "a" * 40)], since=NOW) == OwedLedger()
        assert _artifacts_url(PR_OWED_ARTIFACT, 2) not in gh.calls


class TestCollect:
    def test_first_harvest_starts_one_day_back_and_says_so(self) -> None:
        routes = {
            _artifacts_url(LEDGER_ARTIFACT): _listing([]),
            _pulls_url(): json.dumps([_pull(10, "a" * 40, merged=NOW - 2 * DAY, updated=NOW - 2 * DAY)]).encode(),
        }
        harvest, _, warnings = _harvest(routes)
        assert harvest.collect(NOW) == OwedLedger(harvested_through=NOW)
        assert any("first harvest" in message for message in warnings)
        assert any(_iso(NOW - DAY).replace("Z", "+00:00") in message for message in warnings)

    def test_a_weekly_first_harvest_finds_debt_from_a_pr_merged_days_ago(self) -> None:
        head = "a" * 40
        routes = {
            _artifacts_url(LEDGER_ARTIFACT): _listing([]),
            _pulls_url(): json.dumps([_pull(10, head, merged=NOW - 4 * DAY)]).encode(),
            _artifacts_url(PR_OWED_ARTIFACT): _listing([_artifact(7, created=NOW - 5 * DAY, head_sha=head)]),
            _zip_url(7): _zip(PR_OWED_FILENAME, _ledger(_owed("old_parked")).to_json()),
        }
        gh = FakeGh({f"repos/{REPO}": b'{"id":1,"default_branch":"main"}', **routes})
        harvest = OwedHarvest(gh=gh, repo=REPO, warn=lambda _message: None, first_harvest_lookback=7 * DAY)
        assert harvest.collect(NOW).names == ("old_parked",)

    def test_merges_the_previous_ledger_with_prs_merged_since_its_mark(self) -> None:
        mark = NOW - dt.timedelta(hours=20)
        previous = _ledger(_owed("still_owed", since=NOW - 3 * DAY), harvested_through=mark)
        merged_head, earlier_head = "a" * 40, "b" * 40
        routes = {
            _artifacts_url(LEDGER_ARTIFACT): _listing([_artifact(1, created=mark)]),
            _zip_url(1): _zip(LEDGER_FILENAME, previous.to_json()),
            _pulls_url(): json.dumps(
                [
                    _pull(20, merged_head, merged=NOW - dt.timedelta(hours=2)),
                    _pull(19, earlier_head, merged=mark - dt.timedelta(hours=1)),
                ]
            ).encode(),
            _artifacts_url(PR_OWED_ARTIFACT): _listing(
                [
                    _artifact(8, created=NOW - dt.timedelta(hours=3), head_sha=merged_head),
                    _artifact(7, created=mark - dt.timedelta(hours=2), head_sha=earlier_head),
                ]
            ),
            _zip_url(8): _zip(PR_OWED_FILENAME, _ledger(_owed("pr20_parked", reason=OwedReason.PARKED)).to_json()),
        }
        harvest, gh, warnings = _harvest(routes)
        collected = harvest.collect(NOW)
        assert collected.names == ("pr20_parked", "still_owed")
        assert collected.harvested_through == NOW
        assert _zip_url(7) not in gh.calls
        assert warnings == []

    def test_a_previous_ledger_without_a_mark_falls_back_to_one_day(self) -> None:
        routes = {
            _artifacts_url(LEDGER_ARTIFACT): _listing([_artifact(1, created=NOW - DAY)]),
            _zip_url(1): _zip(LEDGER_FILENAME, _ledger(_owed("kept")).to_json()),
            _pulls_url(): json.dumps([_pull(10, "a" * 40, merged=NOW - dt.timedelta(hours=23))]).encode(),
            _artifacts_url(PR_OWED_ARTIFACT): _listing([]),
        }
        harvest, gh, _ = _harvest(routes)
        assert harvest.collect(NOW).names == ("kept",)
        assert _artifacts_url(PR_OWED_ARTIFACT) in gh.calls

    def test_a_github_api_error_propagates(self) -> None:
        routes = {_artifacts_url(LEDGER_ARTIFACT): _listing([])}
        harvest, _, _ = _harvest(routes)
        with pytest.raises(subprocess.CalledProcessError):
            harvest.collect(NOW)
