"""Tests for ``teatree.loop.pr_ticket_index``."""

from django.test import TestCase

from teatree.core.models.pull_request import PullRequest
from teatree.core.models.ticket import Ticket
from teatree.loop.dispatch import DispatchAction
from teatree.loop.pr_ticket_index import _parse_closes_ticket, build_ticket_index


class TestParseClosesTicket:
    def test_matches_closes_hash_n(self) -> None:
        assert _parse_closes_ticket("Closes #855") == "855"

    def test_matches_fixes_hash_n(self) -> None:
        assert _parse_closes_ticket("Fixes #856 — broken thing") == "856"

    def test_matches_resolves_hash_n(self) -> None:
        assert _parse_closes_ticket("This MR resolves #99") == "99"

    def test_matches_case_insensitive(self) -> None:
        assert _parse_closes_ticket("CLOSES #1") == "1"

    def test_matches_with_colon(self) -> None:
        assert _parse_closes_ticket("Closes: #42") == "42"

    def test_returns_empty_when_no_keyword(self) -> None:
        assert _parse_closes_ticket("Related to #99") == ""

    def test_returns_empty_when_no_hash(self) -> None:
        assert _parse_closes_ticket("Closes nothing in particular") == ""

    def test_returns_first_match_only(self) -> None:
        assert _parse_closes_ticket("Closes #1\nFixes #2") == "1"

    def test_returns_empty_on_empty_description(self) -> None:
        assert _parse_closes_ticket("") == ""


class TestBuildTicketIndexFromFooter:
    """Parser-only path — no DB rows. Index falls back to description scan."""

    def test_uses_closes_footer_when_no_pr_row(self) -> None:
        url = "https://gitlab.com/x/y/-/merge_requests/370"
        actions = [
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #370 open",
                payload={
                    "url": url,
                    "iid": 370,
                    "raw": {"description": "feat(thing): does stuff\n\nCloses #855"},
                },
            ),
        ]
        assert build_ticket_index(actions) == {url: "855"}

    def test_skips_actions_outside_action_needed_inflight(self) -> None:
        url = "https://gitlab.com/x/y/-/merge_requests/370"
        actions = [
            DispatchAction(
                kind="statusline",
                zone="anchors",
                detail="anchor",
                payload={"url": url, "raw": {"description": "Closes #855"}},
            ),
        ]
        assert build_ticket_index(actions) == {}

    def test_skips_non_statusline_actions(self) -> None:
        url = "https://gitlab.com/x/y/-/merge_requests/370"
        actions = [
            DispatchAction(
                kind="agent",
                zone="t3:reviewer",
                detail="Review",
                payload={"url": url, "raw": {"description": "Closes #855"}},
            ),
        ]
        assert build_ticket_index(actions) == {}

    def test_orphan_when_description_has_no_close_keyword(self) -> None:
        url = "https://gitlab.com/x/y/-/merge_requests/371"
        actions = [
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #371 open",
                payload={"url": url, "raw": {"description": "Related to #999"}},
            ),
        ]
        assert build_ticket_index(actions) == {}

    def test_supports_github_body_field(self) -> None:
        url = "https://github.com/owner/repo/pull/42"
        actions = [
            DispatchAction(
                kind="statusline",
                zone="action_needed",
                detail="PR #42 has notes",
                payload={"url": url, "raw": {"body": "Fixes #500"}},
            ),
        ]
        assert build_ticket_index(actions) == {url: "500"}

    def test_handles_missing_raw_payload(self) -> None:
        url = "https://example.com/mr/42"
        actions = [
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #42",
                payload={"url": url, "iid": 42},
            ),
        ]
        assert build_ticket_index(actions) == {}

    def test_returns_empty_for_no_actions(self) -> None:
        assert build_ticket_index([]) == {}


class TestBuildTicketIndexFromDB(TestCase):
    """DB-backed path — ``PullRequest.ticket`` FK is the authoritative source."""

    def test_uses_pull_request_ticket_fk(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://x/issues/855", state="started")
        url = "https://gitlab.com/x/y/-/merge_requests/370"
        PullRequest.objects.create(
            ticket=ticket,
            overlay="acme",
            url=url,
            repo="x/y",
            iid="370",
        )
        actions = [
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #370 open",
                payload={"url": url, "iid": 370, "raw": {}},
            ),
        ]
        assert build_ticket_index(actions) == {url: "855"}

    def test_fk_takes_precedence_over_footer_parse(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://x/issues/855", state="started")
        url = "https://gitlab.com/x/y/-/merge_requests/370"
        PullRequest.objects.create(
            ticket=ticket,
            overlay="acme",
            url=url,
            repo="x/y",
            iid="370",
        )
        actions = [
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #370 open",
                payload={
                    "url": url,
                    "iid": 370,
                    "raw": {"description": "Closes #9999"},
                },
            ),
        ]
        # DB wins — should be 855, not the 9999 from the footer.
        assert build_ticket_index(actions) == {url: "855"}

    def test_mixes_fk_and_footer_per_url(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://x/issues/855", state="started")
        url_db = "https://gitlab.com/x/y/-/merge_requests/370"
        url_parse = "https://gitlab.com/x/y/-/merge_requests/371"
        PullRequest.objects.create(
            ticket=ticket,
            overlay="acme",
            url=url_db,
            repo="x/y",
            iid="370",
        )
        actions = [
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #370",
                payload={"url": url_db, "iid": 370, "raw": {}},
            ),
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #371",
                payload={
                    "url": url_parse,
                    "iid": 371,
                    "raw": {"description": "Fixes #856"},
                },
            ),
        ]
        assert build_ticket_index(actions) == {url_db: "855", url_parse: "856"}


class TestZonesForGroupsByParentTicket(TestCase):
    """End-to-end: ``zones_for`` buckets MRs under parent tickets via FK + footer."""

    def test_in_flight_prs_grouped_under_parent_via_db(self) -> None:
        from teatree.loop.rendering import zones_for  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="acme", issue_url="https://x/issues/855", state="started")
        url_a = "https://gitlab.com/x/y/-/merge_requests/370"
        url_b = "https://gitlab.com/x/y/-/merge_requests/399"
        for iid, url in [("370", url_a), ("399", url_b)]:
            PullRequest.objects.create(ticket=ticket, overlay="acme", url=url, repo="x/y", iid=iid)

        actions = [
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #370 open",
                payload={"url": url_a, "iid": 370, "overlay": "acme", "raw": {}},
            ),
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #399 open",
                payload={"url": url_b, "iid": 399, "overlay": "acme", "raw": {}},
            ),
        ]
        zones = zones_for(actions)
        text = zones.in_flight[0] if isinstance(zones.in_flight[0], str) else zones.in_flight[0].text
        assert "[acme]" in text
        assert "#855:" in text
        assert "!370" in text
        assert "!399" in text

    def test_action_needed_prs_grouped_via_footer_fallback(self) -> None:
        from teatree.loop.rendering import zones_for  # noqa: PLC0415

        # No PullRequest row — description footer is the only source.
        url = "https://gitlab.com/x/y/-/merge_requests/370"
        actions = [
            DispatchAction(
                kind="statusline",
                zone="action_needed",
                detail="PR #370 has 3 notes",
                payload={
                    "url": url,
                    "iid": 370,
                    "draft_count": 3,
                    "overlay": "acme",
                    "raw": {"description": "feat(thing): wip\n\nCloses #855"},
                },
            ),
        ]
        zones = zones_for(actions)
        text = zones.action_needed[0] if isinstance(zones.action_needed[0], str) else zones.action_needed[0].text
        assert "#855:" in text
        assert "!370 (3 notes)" in text

    def test_orphan_pr_renders_under_overlay_without_ticket_prefix(self) -> None:
        from teatree.loop.rendering import zones_for  # noqa: PLC0415

        url = "https://example.com/pr/999"
        actions = [
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #999 open",
                payload={"url": url, "iid": 999, "overlay": "acme", "raw": {}},
            ),
        ]
        zones = zones_for(actions)
        text = zones.in_flight[0] if isinstance(zones.in_flight[0], str) else zones.in_flight[0].text
        assert "[acme]" in text
        assert "!999" in text
        # No parent ticket — should not carry a "#N:" prefix in the bucket.
        assert "#" not in text.replace("[acme]", "")
