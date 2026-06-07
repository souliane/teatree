"""Tests for ``teatree.loop.pr_ticket_index``."""

from django.test import TestCase

from teatree.core.models.pull_request import PullRequest
from teatree.core.models.ticket import Ticket
from teatree.loop.dispatch import DispatchAction
from teatree.loop.pr_ticket_index import _parse_closes_ticket, build_ticket_index, resolve_author_ticket


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


class TestBuildTicketIndexFromTicketExtraPrs(TestCase):
    """#1113 Defect 3 — ``Ticket.extra["prs"]`` is the third resolution source.

    A bare manually-opened MR has no ``PullRequest`` FK row and no
    ``Closes #N`` footer, but the ship pipeline records it under
    ``Ticket.extra["prs"]["<url>"]``. Pre-fix ``build_ticket_index``
    only checked FK + footer and returned ``{}`` for such an MR.
    """

    URL = "https://gitlab.com/souliane/teatree/-/merge_requests/145"

    def _actions(self) -> list[DispatchAction]:
        return [
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #145 open",
                payload={"url": self.URL, "iid": 145, "raw": {}},
            ),
        ]

    def test_resolves_via_ticket_extra_prs_when_no_fk_or_footer(self) -> None:
        Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/142",
            state="started",
            extra={"prs": {self.URL: {"iid": 145}}},
        )
        assert build_ticket_index(self._actions()) == {self.URL: "142"}

    def test_fk_takes_precedence_over_ticket_extra_prs(self) -> None:
        fk_ticket = Ticket.objects.create(overlay="t3-teatree", issue_url="https://x/issues/855", state="started")
        PullRequest.objects.create(ticket=fk_ticket, overlay="t3-teatree", url=self.URL, repo="x/y", iid="145")
        Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/142",
            state="started",
            extra={"prs": {self.URL: {"iid": 145}}},
        )
        # FK (855) outranks the extra["prs"] mapping (142).
        assert build_ticket_index(self._actions()) == {self.URL: "855"}

    def test_footer_takes_precedence_over_ticket_extra_prs(self) -> None:
        Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/142",
            state="started",
            extra={"prs": {self.URL: {"iid": 145}}},
        )
        actions = [
            DispatchAction(
                kind="statusline",
                zone="in_flight",
                detail="PR #145 open",
                payload={"url": self.URL, "iid": 145, "raw": {"description": "Closes #856"}},
            ),
        ]
        assert build_ticket_index(actions) == {self.URL: "856"}

    def test_empty_actions_skip_ticket_extra_lookup(self) -> None:
        """Empty URL set must short-circuit without touching the DB."""
        from teatree.loop.pr_ticket_index import _lookup_ticket_extra_prs  # noqa: PLC0415

        assert _lookup_ticket_extra_prs([]) == {}
        assert _lookup_ticket_extra_prs([""]) == {}

    def test_ticket_with_non_dict_extra_prs_is_skipped(self) -> None:
        """Malformed ``extra["prs"]`` (not a dict) must be ignored, not crash."""
        Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/200",
            state="started",
            extra={"prs": "not-a-dict"},
        )
        assert build_ticket_index(self._actions()) == {}

    def test_ticket_with_blank_issue_url_is_skipped(self) -> None:
        """A ticket whose ``issue_url`` is empty has ticket_number = str(pk).

        ``ticket_number`` falls back to ``pk`` for blank URLs, so the
        guard is on "no number" — only triggered by a hypothetical empty
        pk + empty URL combo. The realistic skip is "no matching URL in
        the prs dict", verified here with a URL the prs map doesn't carry.
        """
        Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="",
            state="started",
            extra={"prs": {"https://other/mr/1": {"iid": 1}}},
        )
        assert build_ticket_index(self._actions()) == {}

    def test_ticket_extra_lookup_failing_apps_get_model_fails_open(self) -> None:
        """A non-ready Django apps registry must collapse to {} silently."""
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.loop.pr_ticket_index import _lookup_ticket_extra_prs  # noqa: PLC0415

        with patch("django.apps.apps.get_model", side_effect=LookupError("boom")):
            assert _lookup_ticket_extra_prs({"https://x/mr/1"}) == {}

    def test_ticket_extra_lookup_failing_query_fails_open(self) -> None:
        """A DB query that raises must collapse to {} silently."""
        from types import SimpleNamespace  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.loop.pr_ticket_index import _lookup_ticket_extra_prs  # noqa: PLC0415

        def _raise(**_kwargs: object) -> object:
            msg = "db down"
            raise RuntimeError(msg)

        broken = SimpleNamespace(objects=SimpleNamespace(exclude=_raise))
        with patch("django.apps.apps.get_model", return_value=broken):
            assert _lookup_ticket_extra_prs({"https://x/mr/1"}) == {}

    def test_ticket_with_falsy_ticket_number_is_skipped(self) -> None:
        """A ticket with ``ticket_number`` resolving falsy must be skipped without crash."""
        from types import SimpleNamespace  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.loop.pr_ticket_index import _lookup_ticket_extra_prs  # noqa: PLC0415

        stub = SimpleNamespace(
            ticket_number="",
            extra={"prs": {"https://x/mr/1": {"iid": 1}}},
        )

        def _exclude(**_kwargs: object) -> object:
            return SimpleNamespace(only=lambda *_args: [stub])

        model = SimpleNamespace(objects=SimpleNamespace(exclude=_exclude))
        with patch("django.apps.apps.get_model", return_value=model):
            assert _lookup_ticket_extra_prs({"https://x/mr/1"}) == {}


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
        assert "!370" in text
        # Annotation chunk removed by #1377 — chip is bare.
        assert "(3 notes)" not in text, repr(text)

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


class TestResolveAuthorTicket(TestCase):
    """#2104 — resolve a PR back to its AUTHOR/delivery ticket, not the PR-url ticket."""

    SLUG = "souliane/teatree"
    PR_ID = 6230
    PR_URL = "https://github.com/souliane/teatree/pull/6230"

    def test_resolves_via_pull_request_fk(self) -> None:
        author = Ticket.objects.create(overlay="t3-teatree", issue_url=f"https://github.com/{self.SLUG}/issues/2104")
        PullRequest.objects.create(
            ticket=author, overlay="t3-teatree", url=self.PR_URL, repo=self.SLUG, iid=str(self.PR_ID)
        )
        resolved = resolve_author_ticket(slug=self.SLUG, pr_id=self.PR_ID, pr_url=self.PR_URL)
        assert resolved is not None
        assert resolved.pk == author.pk

    def test_resolves_via_ticket_extra_prs_fallback(self) -> None:
        author = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url=f"https://github.com/{self.SLUG}/issues/2104",
            extra={"prs": {self.PR_URL: {"draft": False}}},
        )
        resolved = resolve_author_ticket(slug=self.SLUG, pr_id=self.PR_ID, pr_url=self.PR_URL)
        assert resolved is not None
        assert resolved.pk == author.pk

    def test_does_not_match_the_reviewer_ticket_keyed_by_pr_url(self) -> None:
        # The shape AutoReviewDispatch._create_reviewing_task mints — issue_url
        # IS the PR url, no PullRequest FK, no extra["prs"]. It must NOT resolve
        # as the author ticket (the bug the cold review caught).
        Ticket.objects.create(overlay="t3-teatree", issue_url=self.PR_URL)
        resolved = resolve_author_ticket(slug=self.SLUG, pr_id=self.PR_ID, pr_url=self.PR_URL)
        assert resolved is None

    def test_returns_none_when_no_link_exists(self) -> None:
        resolved = resolve_author_ticket(slug=self.SLUG, pr_id=self.PR_ID, pr_url=self.PR_URL)
        assert resolved is None

    def test_no_fk_and_blank_pr_url_skips_extra_prs_fallback(self) -> None:
        # No PullRequest FK and an empty pr_url: the extra["prs"] fallback needs
        # a url key to match, so the resolver returns None without walking.
        Ticket.objects.create(
            overlay="t3-teatree",
            issue_url=f"https://github.com/{self.SLUG}/issues/2104",
            extra={"prs": {self.PR_URL: {"draft": False}}},
        )
        resolved = resolve_author_ticket(slug=self.SLUG, pr_id=self.PR_ID, pr_url="")
        assert resolved is None

    def test_non_dict_prs_extra_is_skipped(self) -> None:
        # A ticket whose extra["prs"] is not a dict must be skipped, not crash.
        Ticket.objects.create(
            overlay="t3-teatree",
            issue_url=f"https://github.com/{self.SLUG}/issues/2104",
            extra={"prs": "garbage"},
        )
        resolved = resolve_author_ticket(slug=self.SLUG, pr_id=self.PR_ID, pr_url=self.PR_URL)
        assert resolved is None

    def test_app_not_ready_degrades_to_none(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch("django.apps.apps.get_model", side_effect=LookupError("boom")):
            assert resolve_author_ticket(slug=self.SLUG, pr_id=self.PR_ID, pr_url=self.PR_URL) is None

    def test_db_query_error_degrades_to_none(self) -> None:
        from types import SimpleNamespace  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        def _raise(**_kwargs: object) -> object:
            msg = "db down"
            raise RuntimeError(msg)

        broken = SimpleNamespace(objects=SimpleNamespace(filter=_raise))
        with patch("django.apps.apps.get_model", return_value=broken):
            assert resolve_author_ticket(slug=self.SLUG, pr_id=self.PR_ID, pr_url=self.PR_URL) is None
