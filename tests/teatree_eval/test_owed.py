import datetime as dt
import io
import json
import subprocess
import zipfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from teatree.eval.owed import (
    LEDGER_ARTIFACT,
    LEDGER_FILENAME,
    LEG_OWED_PREFIX,
    MAX_LISTING_PAGES,
    PAGE_SIZE,
    PR_ARTIFACT_LOOKBACK,
    PR_OWED_ARTIFACT,
    PR_OWED_FILENAME,
    OwedHarvest,
    OwedLedger,
    OwedLedgerError,
    OwedReason,
    OwedScenario,
)

REPO = "souliane/teatree"
NOW = dt.datetime(2026, 10, 16, 3, 0, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)


def test_owed_producer_artifact_names_match_harvest_contract() -> None:
    workflows = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    cases = (
        (
            "eval.yml",
            "Upload the weekly leg's owed scenarios",
            LEG_OWED_PREFIX + "${{ steps.artifact.outputs.suffix }}",
        ),
        (
            "eval-ci-heal.yml",
            "Upload this shard's owed scenarios",
            LEG_OWED_PREFIX + "heal-${{ needs.prepare.outputs.sha }}-${{ steps.artifact.outputs.suffix }}",
        ),
        ("eval-pr.yml", "Upload the owed scenarios", PR_OWED_ARTIFACT),
        ("eval-pr-reusable.yml", "Upload the owed scenarios", PR_OWED_ARTIFACT),
    )
    for filename, step_name, expected in cases:
        jobs = yaml.safe_load((workflows / filename).read_text(encoding="utf-8"))["jobs"]
        uploads = [step for job in jobs.values() for step in job.get("steps", []) if step.get("name") == step_name]
        assert len(uploads) == 1, filename
        assert uploads[0]["with"]["name"] == expected, filename


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

    def test_duplicate_keeps_the_later_attempt_when_the_existing_debt_is_older(self) -> None:
        old = replace(_owed("alpha", since=NOW - DAY), last_attempted=NOW - DAY)
        recent = replace(_owed("alpha", since=NOW), last_attempted=NOW)
        assert _ledger(old).merged(_ledger(recent)).scenarios == (replace(old, last_attempted=NOW),)

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
        assert settled.names == ("new_park", "old_red")
        assert settled.next_batch(1, {"old_red", "new_park"}) == ("new_park",)
        assert OwedLedger.from_json(settled.to_json()).next_batch(1, {"old_red", "new_park"}) == ("new_park",)
        reds = tuple(_owed(f"red_{index}", since=NOW - 2 * DAY) for index in range(14))
        full = _ledger(*reds, _owed("new_park", since=NOW - DAY))
        retried, _ = full.settled([], known=full.names, attempted=[row.name for row in reds], now=NOW)
        assert "new_park" in retried.next_batch(14, full.names)


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
        "name": PR_OWED_ARTIFACT,
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


def _workflows_url() -> str:
    return f"repos/{REPO}/actions/workflows?per_page={PAGE_SIZE}&page=1"


def _workflows(*workflows: dict) -> bytes:
    return json.dumps({"total_count": len(workflows), "workflows": list(workflows)}).encode()


def _runs_url(workflow_id: int, page: int = 1) -> str:
    return f"repos/{REPO}/actions/workflows/{workflow_id}/runs?per_page={PAGE_SIZE}&page={page}"


def _head_runs_url(sha: str) -> str:
    return f"repos/{REPO}/actions/runs?head_sha={sha}&per_page={PAGE_SIZE}&page=1"


def _runs(*runs: dict) -> bytes:
    return json.dumps({"total_count": len(runs), "workflow_runs": list(runs)}).encode()


def _run(
    run_id: int,
    *,
    created: dt.datetime,
    updated: dt.datetime | None = None,
    head_sha: str = "f" * 40,
    branch: str = "main",
) -> dict:
    return {
        "id": run_id,
        "created_at": _iso(created),
        "updated_at": _iso(updated or created),
        "head_sha": head_sha,
        "head_branch": branch,
    }


def _run_artifacts_url(run_id: int) -> str:
    return f"repos/{REPO}/actions/runs/{run_id}/artifacts?per_page={PAGE_SIZE}&page=1"


_WEEKLY_WORKFLOW = {"id": 11, "path": ".github/workflows/eval.yml"}
_HEAL_WORKFLOW = {"id": 12, "path": ".github/workflows/eval-ci-heal.yml"}
_WEEKLY_LEG = "eval-owed-clean_room-1-2"


def _harvest(routes: dict[str, bytes]) -> tuple[OwedHarvest, FakeGh, list[str]]:
    gh = FakeGh({f"repos/{REPO}": b'{"id":1,"default_branch":"main"}', _workflows_url(): _workflows(), **routes})
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
    def test_no_merged_pr_reads_nothing(self) -> None:
        harvest, gh, _ = _harvest({})
        assert harvest.pr_owed([]) == OwedLedger()
        assert gh.calls == []

    def test_fork_artifact_on_a_merged_head_cannot_poison_pr_debt(self) -> None:
        head = "a" * 40
        trusted = _ledger(_owed("trusted"))
        routes = {
            _head_runs_url(head): _runs(_run(77, created=NOW, head_sha=head, branch="feature")),
            _run_artifacts_url(77): _listing(
                [
                    _artifact(2, created=NOW, head_sha=head, provenance=("feature", 2)),
                    _artifact(1, created=NOW - DAY, head_sha=head, provenance=("feature", 1)),
                ]
            ),
            _zip_url(1): _zip(PR_OWED_FILENAME, trusted.to_json()),
        }
        harvest, gh, _ = _harvest(routes)
        assert harvest.pr_owed([(10, head)]) == trusted
        assert _zip_url(2) not in gh.calls

    def test_only_the_final_head_artifact_is_harvested(self) -> None:
        final, older = "a" * 40, "0" * 40
        routes = {
            _head_runs_url(final): _runs(_run(77, created=NOW, head_sha=final, branch="feature")),
            _run_artifacts_url(77): _listing(
                [_artifact(5, created=NOW - DAY, head_sha=final), _artifact(4, created=NOW, head_sha=older)]
            ),
            _zip_url(5): _zip(PR_OWED_FILENAME, _ledger(_owed("final")).to_json()),
        }
        harvest, gh, _ = _harvest(routes)
        assert harvest.pr_owed([(10, final)]).names == ("final",)
        assert _zip_url(4) not in gh.calls

    def test_newest_final_head_rerun_wins(self) -> None:
        head = "a" * 40
        routes = {
            _head_runs_url(head): _runs(_run(77, created=NOW, head_sha=head, branch="feature")),
            _run_artifacts_url(77): _listing(
                [_artifact(6, created=NOW - DAY, head_sha=head), _artifact(7, created=NOW, head_sha=head)]
            ),
            _zip_url(7): _zip(PR_OWED_FILENAME, _ledger(_owed("newest")).to_json()),
        }
        harvest, _, _ = _harvest(routes)
        assert harvest.pr_owed([(10, head)]).names == ("newest",)

    def test_expired_final_head_artifact_warns(self) -> None:
        head = "a" * 40
        routes = {
            _head_runs_url(head): _runs(_run(77, created=NOW, head_sha=head, branch="feature")),
            _run_artifacts_url(77): _listing([_artifact(5, created=NOW, head_sha=head, expired=True)]),
        }
        harvest, _, warnings = _harvest(routes)
        assert harvest.pr_owed([(42, head)]) == OwedLedger()
        assert len(warnings) == 1
        assert "PR #42" in warnings[0]

    def test_no_runs_for_a_merged_head_owe_nothing(self) -> None:
        harvest, gh, _ = _harvest({_head_runs_url("a" * 40): _runs()})
        assert harvest.pr_owed([(42, "a" * 40)]) == OwedLedger()
        assert gh.calls[-1] == _head_runs_url("a" * 40)

    def test_nightly_harvest_queries_only_the_merged_head_even_for_an_old_artifact(self) -> None:
        head = "a" * 40
        routes = {
            _head_runs_url(head): _runs(_run(77, created=NOW - 80 * DAY, head_sha=head, branch="feature")),
            _run_artifacts_url(77): _listing([_artifact(7, created=NOW - 80 * DAY, head_sha=head)]),
            _zip_url(7): _zip(PR_OWED_FILENAME, _ledger(_owed("still_owed")).to_json()),
        }
        harvest, gh, _ = _harvest(routes)
        assert harvest.pr_owed([(42, head)]).names == ("still_owed",)
        assert _artifacts_url(PR_OWED_ARTIFACT) not in gh.calls


class TestLegOwed:
    def test_a_default_branch_leg_artifact_is_owed_once_after_the_mark(self) -> None:
        mark = NOW - DAY
        routes = {
            _workflows_url(): _workflows(_WEEKLY_WORKFLOW, {"id": 13, "path": ".github/workflows/ci.yml"}),
            _runs_url(11): _runs(
                _run(100, created=mark - dt.timedelta(hours=2), updated=NOW),
                _run(101, created=mark - DAY, updated=mark),
            ),
            _run_artifacts_url(100): _listing(
                [
                    _artifact(2, created=NOW) | {"name": _WEEKLY_LEG},
                    _artifact(3, created=mark - dt.timedelta(hours=1)) | {"name": "eval-owed-under_load-1-1"},
                    _artifact(4, created=NOW) | {"name": LEDGER_ARTIFACT},
                    _artifact(5, created=NOW) | {"name": "eval-report-clean_room-1-2"},
                    _artifact(6, created=NOW, provenance=("main", 2)) | {"name": _WEEKLY_LEG},
                ]
            ),
            _zip_url(2): _zip(f"{_WEEKLY_LEG}.json", _ledger(_owed("weekly")).to_json()),
        }
        harvest, gh, _ = _harvest(routes)
        assert harvest.leg_owed(set(), mark).names == ("weekly",)
        assert not {_zip_url(3), _zip_url(4), _zip_url(5), _zip_url(6), _run_artifacts_url(101)} & set(gh.calls)
        assert _runs_url(13) not in gh.calls

    def test_a_heal_leg_on_a_pr_branch_is_owed_only_at_a_merged_final_head(self) -> None:
        final, superseded = "a" * 40, "b" * 40
        routes = {
            _workflows_url(): _workflows(_HEAL_WORKFLOW),
            _runs_url(12): _runs(
                _run(201, created=NOW - 2 * DAY, updated=NOW, head_sha=superseded, branch="feature"),
                _run(200, created=NOW - 3 * DAY, head_sha=final, branch="feature"),
            ),
            _run_artifacts_url(200): _listing(
                [
                    _artifact(7, created=NOW - 3 * DAY, head_sha=final, provenance=("feature", 1))
                    | {"name": f"eval-owed-heal-{final}-subset"}
                ]
            ),
            _zip_url(7): _zip("eval-owed-subset.json", _ledger(_owed("heal")).to_json()),
        }
        harvest, gh, _ = _harvest(routes)
        assert harvest.leg_owed({final}, NOW - DAY).names == ("heal",)
        assert _run_artifacts_url(201) not in gh.calls

    def test_an_expired_leg_artifact_warns_and_owes_nothing(self) -> None:
        routes = {
            _workflows_url(): _workflows(_WEEKLY_WORKFLOW),
            _runs_url(11): _runs(_run(100, created=NOW)),
            _run_artifacts_url(100): _listing([_artifact(2, created=NOW, expired=True) | {"name": _WEEKLY_LEG}]),
        }
        harvest, gh, warnings = _harvest(routes)
        assert harvest.leg_owed(set(), NOW - DAY) == OwedLedger()
        assert _zip_url(2) not in gh.calls
        assert any(_WEEKLY_LEG in message and "expired" in message for message in warnings)

    @pytest.mark.parametrize("members", [["notes.txt"], [f"{_WEEKLY_LEG}.json", "eval-owed-other.json"]])
    def test_a_leg_artifact_without_exactly_one_owed_json_fails_loud(self, members: list[str]) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for member in members:
                archive.writestr(member, OwedLedger().to_json())
        routes = {
            _workflows_url(): _workflows(_WEEKLY_WORKFLOW),
            _runs_url(11): _runs(_run(100, created=NOW)),
            _run_artifacts_url(100): _listing([_artifact(2, created=NOW) | {"name": _WEEKLY_LEG}]),
            _zip_url(2): buffer.getvalue(),
        }
        harvest, _, _ = _harvest(routes)
        with pytest.raises(OwedLedgerError, match="one owed JSON"):
            harvest.leg_owed(set(), NOW - DAY)

    def test_run_paging_stops_at_the_artifact_lookback(self) -> None:
        ancient = NOW - DAY - PR_ARTIFACT_LOOKBACK - DAY
        routes = {
            _workflows_url(): _workflows(_WEEKLY_WORKFLOW),
            _runs_url(11): _runs(*(_run(1000 + i, created=ancient, branch="feature") for i in range(PAGE_SIZE))),
        }
        harvest, gh, _ = _harvest(routes)
        assert harvest.leg_owed(set(), NOW - DAY) == OwedLedger()
        assert _runs_url(11, page=2) not in gh.calls


class TestCollect:
    def test_weekly_and_heal_leg_debt_enters_the_ledger(self) -> None:
        mark = NOW - DAY
        final = "a" * 40
        routes = {
            _artifacts_url(LEDGER_ARTIFACT): _listing([_artifact(1, created=mark)]),
            _zip_url(1): _zip(LEDGER_FILENAME, _ledger(harvested_through=mark).to_json()),
            _pulls_url(): json.dumps([_pull(10, final, merged=NOW - dt.timedelta(hours=2))]).encode(),
            _head_runs_url(final): _runs(_run(200, created=NOW - 3 * DAY, head_sha=final, branch="feature")),
            _workflows_url(): _workflows(_WEEKLY_WORKFLOW, _HEAL_WORKFLOW),
            _runs_url(11): _runs(_run(100, created=NOW)),
            _run_artifacts_url(100): _listing([_artifact(2, created=NOW) | {"name": _WEEKLY_LEG}]),
            _zip_url(2): _zip(f"{_WEEKLY_LEG}.json", _ledger(_owed("weekly_cut_off")).to_json()),
            _runs_url(12): _runs(_run(200, created=NOW - 3 * DAY, head_sha=final, branch="feature")),
            _run_artifacts_url(200): _listing(
                [_artifact(3, created=NOW - 3 * DAY, head_sha=final) | {"name": f"eval-owed-heal-{final}-subset"}]
            ),
            _zip_url(3): _zip("eval-owed-subset.json", _ledger(_owed("heal_cut_off")).to_json()),
        }
        harvest, _, _ = _harvest(routes)
        assert harvest.collect(NOW).names == ("heal_cut_off", "weekly_cut_off")

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
            _head_runs_url(head): _runs(_run(70, created=NOW - 5 * DAY, head_sha=head, branch="feature")),
            _run_artifacts_url(70): _listing([_artifact(7, created=NOW - 5 * DAY, head_sha=head)]),
            _zip_url(7): _zip(PR_OWED_FILENAME, _ledger(_owed("old_parked")).to_json()),
        }
        gh = FakeGh({f"repos/{REPO}": b'{"id":1,"default_branch":"main"}', _workflows_url(): _workflows(), **routes})
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
            _head_runs_url(merged_head): _runs(
                _run(80, created=NOW - dt.timedelta(hours=3), head_sha=merged_head, branch="feature")
            ),
            _run_artifacts_url(80): _listing(
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
            _head_runs_url("a" * 40): _runs(),
        }
        harvest, gh, _ = _harvest(routes)
        assert harvest.collect(NOW).names == ("kept",)
        assert _head_runs_url("a" * 40) in gh.calls
