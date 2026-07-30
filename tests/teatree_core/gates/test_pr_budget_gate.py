"""Per-(repo, ticket) open-PR budget gate — north-star PR-2 proof-case mechanism.

The gate reads ``max_open_prs_per_repo_per_ticket`` as data (constraint-as-data)
and refuses opening a PR when a ticket already has that many open (not-merged)
PRs in one repo. Anti-vacuity coverage. TestUnlimitedOptOut: the ``0`` opt-out
never refuses even with open PRs present (RED if the ``limit <= 0`` guard goes).
TestPerTicketPerRepoScope: at limit 1 a second open PR for the same
``(repo, ticket)`` is refused while the first, a different ticket's PR in the
same repo, and the same ticket's PR in a different repo are all allowed.
TestResolvePerOverlay: the limit flows through the real ``get_effective_settings``
per overlay. TestShippedDefault (D9): the shipped default is ``1``, so a second
open PR is refused out of the box and ``0`` restores the unlimited opt-out.
"""

import time

import httpx
import pytest
from django.test import TestCase

from teatree.core.gates.pr_budget_forge import _forge_cache, reset_forge_pr_budget_cache
from teatree.core.gates.pr_budget_gate import (
    PrBudgetExceededError,
    check_pr_budget,
    count_open_prs_for_repo,
    open_pr_urls_for_repo,
    resolve_pr_budget,
)
from teatree.core.models import ConfigSetting, PullRequest, Ticket
from teatree.utils.run import CommandFailedError

_REPO_A = "souliane/teatree"
_REPO_B = "souliane/other"


def _gh_pr(*, repo: str, number: int, body: str) -> dict:
    return {"html_url": f"https://github.com/{repo}/pull/{number}", "number": number, "body": body}


class _FakeHost:
    """Stub ``CodeHostBackend`` returning a fixed open-PR list for the forge backstop."""

    def __init__(self, prs: list[dict], *, user: str = "fleet-bot", error: Exception | None = None) -> None:
        self._prs = prs
        self._user = user
        self._error = error

    def current_user(self) -> str:
        return self._user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[dict]:
        del author, updated_after
        if self._error is not None:
            raise self._error
        return list(self._prs)


def _pr(ticket: Ticket, *, repo: str, iid: str, state: str = PullRequest.State.OPEN) -> PullRequest:
    return PullRequest.objects.create(
        ticket=ticket,
        url=f"https://github.com/{repo}/pull/{iid}",
        repo=repo,
        iid=iid,
        overlay=ticket.overlay,
        state=state,
    )


class TestCountOpenPrsForRepo(TestCase):
    def test_counts_open_rows_scoped_to_ticket_and_repo(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        _pr(ticket, repo=_REPO_A, iid="2")
        assert count_open_prs_for_repo(ticket, _REPO_A) == 2

    def test_excludes_merged_rows(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1", state=PullRequest.State.MERGED)
        assert count_open_prs_for_repo(ticket, _REPO_A) == 0

    def test_excludes_other_repo_and_other_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        other = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_B, iid="1")
        _pr(other, repo=_REPO_A, iid="2")
        assert count_open_prs_for_repo(ticket, _REPO_A) == 0

    def test_unions_pr_url_by_branch_for_matching_repo(self) -> None:
        # A PR the reconciler has not yet upserted into a PullRequest row is
        # still in ``extra["pr_url_by_branch"]`` (written synchronously by the
        # ship executor) — so the union counts it.
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.IN_REVIEW,
            extra={"pr_url_by_branch": {"feat/x": f"https://github.com/{_REPO_A}/pull/9"}},
        )
        assert count_open_prs_for_repo(ticket, _REPO_A) == 1

    def test_pr_url_by_branch_for_other_repo_does_not_count(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.IN_REVIEW,
            extra={"pr_url_by_branch": {"feat/x": f"https://github.com/{_REPO_B}/pull/9"}},
        )
        assert count_open_prs_for_repo(ticket, _REPO_A) == 0

    def test_union_dedups_a_url_present_in_both_sources(self) -> None:
        url = f"https://github.com/{_REPO_A}/pull/1"
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.IN_REVIEW,
            extra={"pr_url_by_branch": {"feat/x": url}},
        )
        _pr(ticket, repo=_REPO_A, iid="1")  # same url as the extra entry
        assert count_open_prs_for_repo(ticket, _REPO_A) == 1


class TestUnlimitedOptOut(TestCase):
    def test_zero_limit_never_refuses_even_with_open_prs(self) -> None:
        # ``0`` = the unlimited opt-out: two open PRs for the same (repo, ticket)
        # do NOT trip the gate. Anti-vacuous: without the ``limit <= 0``
        # short-circuit, ``count (2) >= 0`` would raise.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        _pr(ticket, repo=_REPO_A, iid="2")
        check_pr_budget(ticket, _REPO_A, limit=0)  # no raise

    def test_negative_limit_is_also_inert(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        check_pr_budget(ticket, _REPO_A, limit=-1)  # no raise


class TestPerTicketPerRepoScope(TestCase):
    def test_second_open_pr_for_same_repo_ticket_is_refused_at_limit_one(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        with pytest.raises(PrBudgetExceededError):
            check_pr_budget(ticket, _REPO_A, limit=1)

    def test_first_open_pr_is_allowed_at_limit_one(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        check_pr_budget(ticket, _REPO_A, limit=1)  # count 0 < 1 -> allowed

    def test_different_ticket_same_repo_is_allowed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        other = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        check_pr_budget(other, _REPO_A, limit=1)  # other ticket has 0 -> allowed

    def test_same_ticket_different_repo_is_allowed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        check_pr_budget(ticket, _REPO_B, limit=1)  # repo B has 0 -> allowed

    def test_merged_pr_does_not_consume_the_budget(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1", state=PullRequest.State.MERGED)
        check_pr_budget(ticket, _REPO_A, limit=1)  # merged excluded -> allowed

    def test_refusal_message_names_the_offending_url_repo_and_escape(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        pr = _pr(ticket, repo=_REPO_A, iid="1")
        with pytest.raises(PrBudgetExceededError) as excinfo:
            check_pr_budget(ticket, _REPO_A, limit=1)
        message = str(excinfo.value)
        assert pr.url in message
        assert _REPO_A in message
        assert "max_open_prs_per_repo_per_ticket" in message


class TestOpenPrUrlsForRepo(TestCase):
    def test_returns_the_deduped_union_url_set(self) -> None:
        url = f"https://github.com/{_REPO_A}/pull/1"
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.IN_REVIEW,
            extra={"pr_url_by_branch": {"feat/x": url, "feat/y": f"https://github.com/{_REPO_A}/pull/2"}},
        )
        _pr(ticket, repo=_REPO_A, iid="1")  # same url as feat/x
        assert open_pr_urls_for_repo(ticket, _REPO_A) == {url, f"https://github.com/{_REPO_A}/pull/2"}


class TestForgeAuthoritativeBackstop(TestCase):
    """Fleet-safety Stage 3: a sibling instance's forge PR counts against the budget.

    Uses a stubbed code host. The process-global forge memo is reset on BOTH
    sides of every test — ``setUp`` clears any bleed from an earlier test, and
    the ``addCleanup`` guarantees the class never leaks a cached entry into a
    later, pk-recycling test that reads the same memo (TSH-1).
    """

    def setUp(self) -> None:
        reset_forge_pr_budget_cache()
        self.addCleanup(reset_forge_pr_budget_cache)

    def _ticket(self, number: int = 123, *, repo: str = _REPO_A) -> Ticket:
        return Ticket.objects.create(
            overlay="t3-teatree",
            state=Ticket.State.IN_REVIEW,
            issue_url=f"https://github.com/{repo}/issues/{number}",
        )

    def test_blocks_when_forge_shows_a_pr_the_local_db_does_not(self) -> None:
        # (a) RED before the forge backstop: local DB has NO PR rows for the ticket,
        # but the forge reports a sibling instance's open PR footer-linked to it.
        ticket = self._ticket(123)
        host = _FakeHost([_gh_pr(repo=_REPO_A, number=501, body="feat: x\n\nCloses #123")])
        with pytest.raises(PrBudgetExceededError) as excinfo:
            check_pr_budget(ticket, _REPO_A, limit=1, host=host)
        assert "https://github.com/souliane/teatree/pull/501" in str(excinfo.value)

    def test_fails_open_and_allows_on_a_forge_error(self) -> None:
        # (b) A forge outage must not block shipping — degrade to local-only (empty).
        ticket = self._ticket(123)
        host = _FakeHost([], error=CommandFailedError(["gh", "api"], 1, "", "502 Bad Gateway"))
        check_pr_budget(ticket, _REPO_A, limit=1, host=host)  # no raise

    def test_fails_open_and_allows_on_a_gitlab_httpx_error(self) -> None:
        # Regression: the GitLab backend raises httpx.HTTPError, which is neither
        # OSError nor ValueError. A narrow catch would let it propagate and
        # hard-block every ship — the fail-CLOSED inversion this backstop prevents.
        ticket = self._ticket(123)
        host = _FakeHost([], error=httpx.ReadTimeout("read timed out"))
        check_pr_budget(ticket, _REPO_A, limit=1, host=host)  # no raise -> ship allowed

    def test_does_not_double_count_a_pr_present_in_both_local_db_and_forge(self) -> None:
        # (c) The same PR in the local DB and on the forge is ONE, not two: at
        # limit 2 the single PR is allowed. A DIFFERENT forge PR would make two.
        ticket = self._ticket(123)
        url = f"https://github.com/{_REPO_A}/pull/501"
        PullRequest.objects.create(ticket=ticket, url=url, repo=_REPO_A, iid="501", overlay=ticket.overlay)
        host = _FakeHost([_gh_pr(repo=_REPO_A, number=501, body="Closes #123")])
        check_pr_budget(ticket, _REPO_A, limit=2, host=host)  # deduped -> count 1 < 2

    def test_local_and_a_distinct_forge_pr_together_reach_the_budget(self) -> None:
        # Anti-vacuity companion to (c): a DISTINCT forge PR is not deduped away.
        ticket = self._ticket(123)
        PullRequest.objects.create(
            ticket=ticket,
            url=f"https://github.com/{_REPO_A}/pull/500",
            repo=_REPO_A,
            iid="500",
            overlay=ticket.overlay,
        )
        host = _FakeHost([_gh_pr(repo=_REPO_A, number=501, body="Closes #123")])
        with pytest.raises(PrBudgetExceededError):
            check_pr_budget(ticket, _REPO_A, limit=2, host=host)  # {500, 501} -> 2 >= 2

    def test_respects_the_limit_setting(self) -> None:
        # (d) One forge PR: allowed at limit 2, refused at limit 1.
        ticket = self._ticket(123)
        host = _FakeHost([_gh_pr(repo=_REPO_A, number=501, body="Closes #123")])
        check_pr_budget(ticket, _REPO_A, limit=2, host=host)  # count 1 < 2 -> allowed
        with pytest.raises(PrBudgetExceededError):
            check_pr_budget(ticket, _REPO_A, limit=1, host=host)

    def test_forge_pr_in_another_repo_does_not_block(self) -> None:
        # (e) Repo-scoping: a footer-linked forge PR living in another repo is
        # dropped, so the budget for _REPO_A stays clear.
        ticket = self._ticket(123)
        host = _FakeHost([_gh_pr(repo=_REPO_B, number=7, body="Closes #123")])
        check_pr_budget(ticket, _REPO_A, limit=1, host=host)  # other-repo PR ignored

    def test_forge_pr_in_the_matching_repo_does_block(self) -> None:
        # Anti-vacuity companion to (e): the same PR in the matching repo blocks.
        ticket = self._ticket(123)
        host = _FakeHost([_gh_pr(repo=_REPO_A, number=7, body="Closes #123")])
        with pytest.raises(PrBudgetExceededError):
            check_pr_budget(ticket, _REPO_A, limit=1, host=host)

    def test_inert_at_zero_limit_never_calls_the_forge(self) -> None:
        ticket = self._ticket(123)
        never = "forge must not be consulted at the unlimited opt-out"

        class _Boom:
            def current_user(self) -> str:
                raise AssertionError(never)

            def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[dict]:
                raise AssertionError(never)

        check_pr_budget(ticket, _REPO_A, limit=0, host=_Boom())  # no raise, no forge call


class TestResolvePerOverlay(TestCase):
    @pytest.fixture(autouse=True)
    def _config_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_limit_flows_through_get_effective_settings_per_overlay(self) -> None:
        # Overlay A pins a cap of 2; overlay B pins the unlimited opt-out (0).
        # The per-overlay ConfigSetting row is the sole source, distinct from the
        # shipped default of 1.
        ConfigSetting.objects.set_value("max_open_prs_per_repo_per_ticket", value=2, scope="overlay-a")
        ConfigSetting.objects.set_value("max_open_prs_per_repo_per_ticket", value=0, scope="overlay-b")
        assert resolve_pr_budget("overlay-a") == 2
        assert resolve_pr_budget("overlay-b") == 0

    def test_default_is_one_when_unset(self) -> None:
        # D9: the shipped default is one-open-PR-per-repo-per-ticket, not unlimited.
        assert resolve_pr_budget(None) == 1


class TestShippedDefault(TestCase):
    """D9: the one-ticket-one-PR discipline holds out of the box (default ``1``)."""

    @pytest.fixture(autouse=True)
    def _config_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_second_open_pr_is_refused_at_the_resolved_default(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        limit = resolve_pr_budget(None)
        assert limit == 1
        with pytest.raises(PrBudgetExceededError):
            check_pr_budget(ticket, _REPO_A, limit=limit)

    def test_zero_row_restores_unlimited(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr(ticket, repo=_REPO_A, iid="1")
        _pr(ticket, repo=_REPO_A, iid="2")
        ConfigSetting.objects.set_value("max_open_prs_per_repo_per_ticket", value=0)
        limit = resolve_pr_budget(None)
        assert limit == 0
        check_pr_budget(ticket, _REPO_A, limit=limit)  # unlimited opt-out -> no raise


class TestForgeBudgetMemoIsolation:
    """TSH-1 regression: the forge PR-budget memo resets AFTER every test, not only before.

    Resetting the process-global ``_forge_cache`` solely in ``setUp`` let the
    class's last cache-populating test leak an
    entry into any later test that reads the same pk-keyed memo
    (e.g. the ``pr ensure-pr`` budget check) — the "green locally, red under a
    shard" pollution class, amplified by sqlite pk-recycling colliding a stale
    ``(ticket.pk, repo)`` key onto a fresh ticket. The class now registers an
    ``addCleanup`` so the memo is clean on both sides of every test.
    """

    def test_class_registers_a_teardown_reset_for_the_forge_memo(self) -> None:
        # Anti-vacuous: drive the class's own setUp/cleanup machinery. With the
        # addCleanup present, doCleanups() empties the memo populated between them;
        # without it (the pre-fix state), doCleanups() is a no-op and the entry
        # survives -> RED. The method name is only for a valid TestCase construction;
        # setUp/doCleanups touch no database.
        case = TestForgeAuthoritativeBackstop("test_respects_the_limit_setting")
        try:
            case.setUp()  # clears now AND registers the teardown reset (the fix)
            _forge_cache[1, _REPO_A] = (time.monotonic(), {f"https://github.com/{_REPO_A}/pull/9"})
            case.doCleanups()  # runs the registered addCleanup -> resets the memo
            assert _forge_cache == {}, "TestForgeAuthoritativeBackstop must reset the forge memo in teardown"
        finally:
            reset_forge_pr_budget_cache()
