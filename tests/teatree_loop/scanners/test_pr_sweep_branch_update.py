"""Tests for the sweep's stale-base merge-update remedy (#4063, #4526).

Covers the classification predicate (:func:`red_required_at_stale_base`), the
``gh``-backed write it drives (:meth:`GhPrApiClient.update_pr_branch`), and the
batched ``Ref.compare`` read that decides behind-ness (#4526). The scanner-level
decision ladder and the three bounds are pinned in ``test_pr_sweep_scanner.py``.
"""

import json

from teatree.loop.scanners.pr_sweep_adapters import GhPrApiClient
from teatree.loop.scanners.pr_sweep_decision import red_required_at_stale_base
from teatree.loop.scanners.pr_sweep_types import PrSummary

SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"


class TestRedRequiredAtStaleBase:
    """The rule: a required red is UNKNOWN when the base it judged has moved."""

    def test_red_on_behind_branch_is_stale(self) -> None:
        assert red_required_at_stale_base({"test (3.13)"}, behind_main=True, conflicted=False) is True

    def test_repo_state_red_on_behind_branch_is_stale(self) -> None:
        assert red_required_at_stale_base({"blueprint-cross-pr"}, behind_main=True, conflicted=False) is True

    def test_red_on_up_to_date_branch_is_the_branchs_own_verdict(self) -> None:
        assert red_required_at_stale_base({"test (3.13)"}, behind_main=False, conflicted=False) is False

    def test_no_failing_required_check_is_never_stale(self) -> None:
        assert red_required_at_stale_base(set(), behind_main=True, conflicted=False) is False

    def test_conflicted_branch_needs_resolution_not_a_merge_update(self) -> None:
        # A conflict is also behind, and truthfully reported so — this is what refuses it.
        assert red_required_at_stale_base({"test (3.13)"}, behind_main=True, conflicted=True) is False


class _CapturingClient(GhPrApiClient):
    """A client whose ``gh`` calls are captured instead of executed."""

    __slots__ = ()

    argv_log: "list[list[str]]" = []  # noqa: RUF012 — shared capture buffer; the slotted base forbids an instance field.
    rc: int = 0

    def _run_gh(self, argv: list[str]) -> tuple[int, str, str]:
        type(self).argv_log.append(argv)
        return type(self).rc, "", "expected_head_sha does not match"


class TestGhUpdatePrBranch:
    """The write is SHA-bound, so a force-push in the TOCTOU window is refused."""

    def _client(self, *, rc: int) -> _CapturingClient:
        _CapturingClient.argv_log = []
        _CapturingClient.rc = rc
        return _CapturingClient(token="")

    def test_shells_the_sha_bound_update_branch_endpoint(self) -> None:
        client = self._client(rc=0)

        assert client.update_pr_branch(slug=SLUG, pr_id=4029, expected_head_oid=HEAD) is True

        argv = _CapturingClient.argv_log[0]
        assert argv[:4] == ["api", "--method", "PUT", f"repos/{SLUG}/pulls/4029/update-branch"]
        assert f"expected_head_sha={HEAD}" in argv

    def test_non_zero_rc_reports_refusal(self) -> None:
        client = self._client(rc=1)

        assert client.update_pr_branch(slug=SLUG, pr_id=4029, expected_head_oid=HEAD) is False


#: The login a fork PR's head repository reports — anything but the base repo's owner.
FORK_OWNER = "outsider"


def _raw_pr(
    *,
    number: int,
    merge_state: str,
    head_ref: str = "some-branch",
    base_ref: str = "main",
    cross_repo: bool = False,
) -> dict[str, object]:
    return {
        "number": number,
        "headRefOid": HEAD,
        "url": f"https://github.com/{SLUG}/pull/{number}",
        "title": f"PR {number}",
        "mergeable": "CONFLICTING" if merge_state == "DIRTY" else "MERGEABLE",
        "mergeStateStatus": merge_state,
        "baseRefName": base_ref,
        "headRefName": head_ref,
        "headRepositoryOwner": {"login": FORK_OWNER if cross_repo else "souliane"},
        "isCrossRepository": cross_repo,
        "author": {"login": "souliane"},
    }


def _compare_payload(behind_by_per_pr: dict[int, int | None]) -> str:
    aliases: dict[str, object] = {
        f"p{number}": None if behind is None else {"behindBy": behind} for number, behind in behind_by_per_pr.items()
    }
    return json.dumps({"data": {"repository": {"ref": aliases}}})


class _ScriptedClient(GhPrApiClient):
    """Serves a canned ``pr list`` payload and a canned GraphQL compare payload."""

    __slots__ = ()

    argv_log: "list[list[str]]" = []  # noqa: RUF012 — shared capture buffer; the slotted base forbids an instance field.
    pr_list_json: str = "[]"
    compare_json: str = ""
    compare_rc: int = 0

    def _run_gh(self, argv: list[str]) -> tuple[int, str, str]:
        type(self).argv_log.append(argv)
        if argv[:2] == ["api", "graphql"]:
            return type(self).compare_rc, type(self).compare_json, "Could not resolve head ref"
        return 0, type(self).pr_list_json, ""


class TestListOpenPrsResolvesBehindBase:
    """#4526: behind-ness comes from ``Ref.compare``, never from ``mergeStateStatus``.

    ``mergeStateStatus`` is a SINGLE highest-precedence value, so behind+red reads
    ``BLOCKED`` and behind+conflicted reads ``DIRTY``. Restoring the
    ``== "BEHIND"`` check turns the first two cases here red.
    """

    def _client(
        self,
        raw_prs: list[dict[str, object]],
        *,
        behind: dict[int, int | None],
        compare_rc: int = 0,
    ) -> _ScriptedClient:
        _ScriptedClient.argv_log = []
        _ScriptedClient.pr_list_json = json.dumps(raw_prs)
        _ScriptedClient.compare_json = _compare_payload(behind)
        _ScriptedClient.compare_rc = compare_rc
        return _ScriptedClient(token="")

    def _one(self, raw: dict[str, object], *, behind: dict[int, int | None], rc: int = 0) -> PrSummary:
        client = self._client([raw], behind=behind, compare_rc=rc)
        return client.list_open_prs(slug=SLUG)[0]

    def test_blocked_pr_that_is_behind_is_reported_behind(self) -> None:
        pr = self._one(_raw_pr(number=4597, merge_state="BLOCKED"), behind={4597: 4})

        assert pr.behind_main is True

    def test_clean_pr_that_is_behind_is_reported_behind(self) -> None:
        pr = self._one(_raw_pr(number=4622, merge_state="CLEAN"), behind={4622: 1})

        assert pr.behind_main is True

    def test_up_to_date_pr_is_not_behind(self) -> None:
        pr = self._one(_raw_pr(number=4624, merge_state="CLEAN"), behind={4624: 0})

        assert pr.behind_main is False

    def test_conflicted_pr_reports_its_behind_ness_truthfully(self) -> None:
        pr = self._one(_raw_pr(number=4500, merge_state="DIRTY"), behind={4500: 2})

        assert (pr.is_conflicted, pr.behind_main) == (True, True)

    def test_failed_compare_read_falls_back_to_the_merge_state_signal(self) -> None:
        behind = self._one(_raw_pr(number=1, merge_state="BEHIND"), behind={}, rc=1)
        clean = self._one(_raw_pr(number=2, merge_state="CLEAN"), behind={}, rc=1)

        assert (behind.behind_main, clean.behind_main) == (True, False)

    def test_undetermined_alias_falls_back_rather_than_reporting_up_to_date(self) -> None:
        pr = self._one(_raw_pr(number=3, merge_state="BEHIND"), behind={3: None})

        assert pr.behind_main is True

    def test_one_compare_call_per_distinct_base_ref(self) -> None:
        client = self._client(
            [
                _raw_pr(number=10, merge_state="CLEAN", head_ref="a"),
                _raw_pr(number=11, merge_state="CLEAN", head_ref="b"),
                _raw_pr(number=12, merge_state="CLEAN", head_ref="c", base_ref="release"),
            ],
            behind={10: 0, 11: 0, 12: 0},
        )

        client.list_open_prs(slug=SLUG)

        graphql_calls = [argv for argv in _ScriptedClient.argv_log if argv[:2] == ["api", "graphql"]]
        assert len(graphql_calls) == 2

    def test_cross_repo_head_is_owner_qualified_in_the_query(self) -> None:
        client = self._client(
            [_raw_pr(number=20, merge_state="CLEAN", head_ref="patch", cross_repo=True)],
            behind={20: 0},
        )

        client.list_open_prs(slug=SLUG)

        query = next(argv[-1] for argv in _ScriptedClient.argv_log if argv[:2] == ["api", "graphql"])
        assert f'"{FORK_OWNER}:patch"' in query

    def test_no_open_prs_issues_no_compare_call(self) -> None:
        client = self._client([], behind={})

        assert client.list_open_prs(slug=SLUG) == []
        assert [argv for argv in _ScriptedClient.argv_log if argv[:2] == ["api", "graphql"]] == []

    def test_pr_without_reported_refs_is_left_out_of_the_compare(self) -> None:
        raw = _raw_pr(number=30, merge_state="BEHIND")
        del raw["baseRefName"]
        client = self._client([raw], behind={})

        pr = client.list_open_prs(slug=SLUG)[0]

        assert pr.behind_main is True
        assert [argv for argv in _ScriptedClient.argv_log if argv[:2] == ["api", "graphql"]] == []

    def test_pr_list_requests_the_fields_the_compare_needs(self) -> None:
        client = self._client([], behind={})

        client.list_open_prs(slug=SLUG)

        fields = _ScriptedClient.argv_log[0][-1]
        assert "baseRefName" in fields
        assert "headRefName" in fields
        assert "headRepositoryOwner" in fields
