"""The archived/superseded-page predicate — and where it must answer UNKNOWN."""

import pytest

from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.liveness import Liveness, PageLivenessProbe
from tests.teatree_backends.notion._fake_notion import FakeNotion

SUCCESSOR = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def probe(notion: FakeNotion) -> PageLivenessProbe:
    _ = notion
    return PageLivenessProbe(NotionClient(token="secret"))


class TestTheFlagIsTheOnlyProofOfDeath:
    def test_an_archived_page_is_dead(self, probe: PageLivenessProbe, notion: FakeNotion) -> None:
        notion.page_archived = True

        verdict = probe.verdict(notion.page_id)

        assert verdict.state is Liveness.DEAD
        assert verdict.reason == "archived_flag"
        assert verdict.readable is False

    def test_a_plain_live_page_needs_no_corroboration(self, probe: PageLivenessProbe, notion: FakeNotion) -> None:
        verdict = probe.verdict(notion.page_id)

        assert verdict.state is Liveness.LIVE
        assert verdict.reason == "not_a_database_row"

    def test_a_live_row_its_database_still_returns_is_live(self, probe: PageLivenessProbe, notion: FakeNotion) -> None:
        notion.make_database_row(database_id="db-backlog")

        verdict = probe.verdict(notion.page_id)

        assert verdict.state is Liveness.LIVE
        assert verdict.reason == "present_in_parent_database"

    def test_membership_is_asked_by_the_rows_own_title(self, probe: PageLivenessProbe, notion: FakeNotion) -> None:
        notion.make_database_row(database_id="db-backlog", title_property="Name", title="pricing tooltip")

        probe.verdict(notion.page_id)

        assert notion.query_filters == [{"property": "Name", "title": {"equals": "pricing tooltip"}}]


class TestATitleFilterMissIsNotAbsence:
    def test_a_row_the_title_filter_misses_is_found_by_id_and_reads_as_live(
        self, probe: PageLivenessProbe, notion: FakeNotion
    ) -> None:
        notion.make_database_row(database_id="db-backlog", title="Day 1 sample event 🎯 ")
        notion.title_filter_misses = True

        verdict = probe.verdict(notion.page_id)

        assert verdict.state is Liveness.LIVE
        assert verdict.reason == "present_in_parent_database"

    def test_a_title_opening_with_a_mention_is_matched_on_its_text(
        self, probe: PageLivenessProbe, notion: FakeNotion
    ) -> None:
        notion.make_database_row(database_id="db-backlog")
        date = {"type": "mention", "plain_text": "2026-09-29", "mention": {"type": "date"}}
        text = {"type": "text", "plain_text": " - DEMO - Sample Event Day 1", "text": {"content": " - DEMO"}}
        notion.set_property("Name", {"type": "title", "title": [date, text]})

        verdict = probe.verdict(notion.page_id)

        assert notion.query_filters[0] == {"property": "Name", "title": {"contains": "- DEMO - Sample Event Day 1"}}
        assert verdict.state is Liveness.LIVE

    def test_the_id_scan_follows_every_page_of_results(self, probe: PageLivenessProbe, notion: FakeNotion) -> None:
        notion.make_database_row(database_id="db-backlog")
        notion.rows = [*({"id": f"{number:032x}"} for number in range(1, 250)), {"id": notion.page_id}]
        notion.title_filter_misses = True

        verdict = probe.verdict(notion.page_id)

        assert verdict.state is Liveness.LIVE

    def test_a_row_neither_query_returns_says_what_ran_and_never_blames_sharing(
        self, probe: PageLivenessProbe, notion: FakeNotion
    ) -> None:
        notion.make_database_row(database_id="db-backlog", title="pricing tooltip")
        notion.rows = [{"id": SUCCESSOR}]

        verdict = probe.verdict(notion.page_id)

        assert verdict.reason == "absent_from_parent_database"
        assert "the database query worked but did not return this page among rows titled 'pricing tooltip'" in (
            verdict.detail
        )
        assert "1 row(s) returned" in verdict.detail
        assert "share" not in verdict.detail


class TestAFailedQueryNamesItsRealCause:
    @pytest.fixture(autouse=True)
    def _no_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_NOTION_HTTP_MAX_RETRIES", "0")

    @pytest.mark.parametrize(
        ("status", "code"),
        [(429, "rate_limited"), (503, "service_unavailable"), (400, "validation_error")],
        ids=["rate-limited", "server-error", "unsupported-query"],
    )
    def test_a_query_failure_other_than_not_shared_is_unverified_and_gives_no_sharing_advice(
        self, probe: PageLivenessProbe, notion: FakeNotion, status: int, code: str
    ) -> None:
        notion.make_database_row(database_id="db-backlog")
        notion.query_fail_with = (status, code)

        verdict = probe.verdict(notion.page_id)

        assert verdict.state is Liveness.UNKNOWN
        assert verdict.reason == "parent_database_unverified"
        assert str(status) in verdict.detail
        assert "share" not in verdict.detail

    def test_a_403_says_the_integration_lacks_access_rather_than_retry(
        self, probe: PageLivenessProbe, notion: FakeNotion
    ) -> None:
        notion.make_database_row(database_id="db-backlog")
        notion.query_fail_with = (403, "restricted_resource")

        verdict = probe.verdict(notion.page_id)

        assert verdict.reason == "parent_database_unverified"
        assert "lacks access" in verdict.detail
        assert "retry" not in verdict.detail

    def test_a_failure_of_the_id_scan_after_the_title_query_worked_gives_no_sharing_advice(
        self, probe: PageLivenessProbe, notion: FakeNotion
    ) -> None:
        notion.make_database_row(database_id="db-backlog")
        notion.title_filter_misses = True
        notion.query_fail_on_unfiltered = (429, "rate_limited")

        verdict = probe.verdict(notion.page_id)

        assert verdict.reason == "parent_database_unverified"
        assert "share" not in verdict.detail

    def test_a_parent_database_not_shared_still_names_the_share(
        self, probe: PageLivenessProbe, notion: FakeNotion
    ) -> None:
        notion.make_database_row(database_id="db-backlog")
        notion.query_fail_with = (404, "object_not_found")

        verdict = probe.verdict(notion.page_id)

        assert verdict.reason == "parent_database_unreadable"
        assert "check that the integration can see that database (usually: share it)" in verdict.detail

    def test_the_id_scan_says_when_it_stopped_at_its_cap(self, probe: PageLivenessProbe, notion: FakeNotion) -> None:
        notion.make_database_row(database_id="db-backlog")
        notion.rows = [{"id": f"{number:032x}"} for number in range(1, 1100)]
        notion.title_filter_misses = True

        verdict = probe.verdict(notion.page_id)

        assert verdict.reason == "absent_from_parent_database"
        assert "scanning 1000 row(s)" in verdict.detail
        assert "the scan stopped at its cap" in verdict.detail


class TestAnUncheckableSignalIsUnknownNeverFine:
    @pytest.mark.parametrize(
        ("setup", "reason"),
        [
            ("unreadable_database", "parent_database_unreadable"),
            ("absent_from_database", "absent_from_parent_database"),
            ("no_title", "identity_unresolvable"),
        ],
    )
    def test_each_uncheckable_shape_refuses_rather_than_passing(
        self, probe: PageLivenessProbe, notion: FakeNotion, setup: str, reason: str
    ) -> None:
        notion.make_database_row(database_id="db-backlog")
        if setup == "unreadable_database":
            notion.query_fail_with = (404, "object_not_found")
        elif setup == "absent_from_database":
            notion.rows = [{"id": SUCCESSOR, "url": "https://www.notion.so/2852"}]
        else:
            notion.properties.clear()

        verdict = probe.verdict(notion.page_id)

        assert verdict.state is Liveness.UNKNOWN, "a check that could not reach its subject must never read as live"
        assert verdict.reason == reason
        assert verdict.readable is False


class TestRecovery:
    def test_a_dead_row_names_the_live_row_sharing_its_title(
        self, probe: PageLivenessProbe, notion: FakeNotion
    ) -> None:
        notion.make_database_row(database_id="db-backlog")
        notion.page_archived = True
        notion.rows = [{"id": SUCCESSOR, "url": "https://www.notion.so/2852"}]

        verdict = probe.verdict(notion.page_id)

        assert verdict.successors == ("https://www.notion.so/2852",)
        assert "https://www.notion.so/2852" in verdict.recovery()

    def test_an_archived_sibling_is_not_offered_as_the_successor(
        self, probe: PageLivenessProbe, notion: FakeNotion
    ) -> None:
        notion.make_database_row(database_id="db-backlog")
        notion.page_archived = True
        notion.rows = [{"id": SUCCESSOR, "url": "https://www.notion.so/2852", "archived": True}]

        verdict = probe.verdict(notion.page_id)

        assert verdict.successors == ()
        assert "could NOT be resolved" in verdict.recovery()

    def test_a_failed_successor_lookup_never_softens_the_verdict(
        self, probe: PageLivenessProbe, notion: FakeNotion
    ) -> None:
        notion.make_database_row(database_id="db-backlog")
        notion.page_archived = True
        notion.query_fail_with = (429, "rate_limited")

        verdict = probe.verdict(notion.page_id)

        assert verdict.state is Liveness.DEAD
        assert verdict.successors == ()

    def test_the_refusal_states_the_rule_and_the_audited_escape(
        self, probe: PageLivenessProbe, notion: FakeNotion
    ) -> None:
        notion.page_archived = True

        message = str(probe.verdict(notion.page_id).as_error(notion.page_id))

        assert "it is not a source at all" in message
        assert "t3 notion audit-fetch" in message
        assert message.startswith(f"page {notion.page_id} is dead")


class TestTheClientSurface:
    def test_page_is_live_reports_false_for_unknown_not_only_for_dead(self, notion: FakeNotion) -> None:
        notion.make_database_row(database_id="db-backlog")
        notion.query_fail_with = (404, "object_not_found")

        assert NotionClient(token="secret").page_is_live(notion.page_id) is False

    def test_page_is_live_reports_true_for_a_live_page(self, notion: FakeNotion) -> None:
        assert NotionClient(token="secret").page_is_live(notion.page_id) is True
