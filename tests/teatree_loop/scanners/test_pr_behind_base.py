"""Tests for the batched behind-base compare read (#4526).

``mergeStateStatus`` is a single highest-precedence value, so a PR that is behind
AND red reports ``BLOCKED`` and one that is behind AND conflicted reports
``DIRTY`` — behind-ness cannot be read off it. These pin the pure query-build and
response-parse halves of the ``Ref.compare`` read that replaces it; the ``gh``
call that carries them is pinned in ``test_pr_sweep_branch_update.py``.
"""

import json

from teatree.loop.scanners.pr_behind_base import (
    BEHIND_CHUNK_SIZE,
    build_compare_query,
    chunk_heads,
    head_compare_ref,
    parse_compare_response,
)


def _response(aliases: dict[str, object], *, errors: list[object] | None = None) -> str:
    payload: dict[str, object] = {"data": {"repository": {"ref": aliases}}}
    if errors is not None:
        payload["errors"] = errors
    return json.dumps(payload)


class TestHeadCompareRef:
    """The fork-qualified ``owner:branch`` form is canonical, normalised UP at decode."""

    def test_same_repo_head_stays_bare(self) -> None:
        assert head_compare_ref(head_ref="my-branch", owner="souliane", cross_repo=False) == "my-branch"

    def test_cross_repo_head_is_owner_qualified(self) -> None:
        assert head_compare_ref(head_ref="my-branch", owner="contributor", cross_repo=True) == "contributor:my-branch"

    def test_cross_repo_without_a_reported_owner_stays_bare(self) -> None:
        assert head_compare_ref(head_ref="my-branch", owner="", cross_repo=True) == "my-branch"


class TestBuildCompareQuery:
    """One aliased query per base ref — the whole open-PR set in a single call."""

    def _query(self) -> str:
        return build_compare_query(
            owner="souliane",
            name="teatree",
            base_ref="main",
            heads={4597: "fix-a", 4622: "fix-b"},
        )

    def test_names_the_repository_and_the_qualified_base_ref(self) -> None:
        query = self._query()

        assert 'repository(owner: "souliane", name: "teatree")' in query
        assert 'ref(qualifiedName: "refs/heads/main")' in query

    def test_carries_one_behind_by_alias_per_pr(self) -> None:
        query = self._query()

        assert 'p4597: compare(headRef: "fix-a") { behindBy }' in query
        assert 'p4622: compare(headRef: "fix-b") { behindBy }' in query

    def test_escapes_a_branch_name_that_would_break_out_of_the_literal(self) -> None:
        query = build_compare_query(owner="o", name="n", base_ref="main", heads={1: 'we"ird\\'})

        assert 'compare(headRef: "we\\"ird\\\\")' in query


class TestParseCompareResponse:
    """behindBy is the answer; anything unreadable is UNDETERMINED, never False."""

    def test_positive_behind_by_on_a_blocked_pr_is_behind(self) -> None:
        assert parse_compare_response(_response({"p4597": {"behindBy": 4}})) == {4597: True}

    def test_zero_behind_by_is_up_to_date(self) -> None:
        assert parse_compare_response(_response({"p4624": {"behindBy": 0}})) == {4624: False}

    def test_null_alias_is_undetermined_not_up_to_date(self) -> None:
        assert parse_compare_response(_response({"p999": None})) == {999: None}

    def test_reads_the_data_that_came_back_beside_a_partial_error(self) -> None:
        payload = _response(
            {"p4597": {"behindBy": 4}, "p999": None},
            errors=[{"type": "NOT_FOUND", "message": "Could not resolve head ref 'gone'."}],
        )

        assert parse_compare_response(payload) == {4597: True, 999: None}

    def test_non_integer_behind_by_is_undetermined(self) -> None:
        assert parse_compare_response(_response({"p1": {"behindBy": "4"}})) == {1: None}
        assert parse_compare_response(_response({"p2": {}})) == {2: None}

    def test_unresolvable_base_ref_yields_no_answers(self) -> None:
        assert parse_compare_response(json.dumps({"data": {"repository": {"ref": None}}})) == {}

    def test_malformed_or_unexpected_payloads_yield_no_answers(self) -> None:
        assert parse_compare_response("not json at all") == {}
        assert parse_compare_response("[]") == {}
        assert parse_compare_response("") == {}

    def test_non_alias_keys_are_ignored(self) -> None:
        aliases = {"name": "main", "pnotanumber": {"behindBy": 9}, "p7": {"behindBy": 1}}

        assert parse_compare_response(_response(aliases)) == {7: True}

    def test_boolean_behind_by_is_undetermined(self) -> None:
        # bool is an int subclass; True would otherwise read as "1 commit behind".
        assert parse_compare_response(_response({"p1": {"behindBy": True}})) == {1: None}

    def test_payload_without_the_expected_envelope_yields_no_answers(self) -> None:
        assert parse_compare_response(json.dumps({})) == {}
        assert parse_compare_response(json.dumps({"data": "nope"})) == {}
        assert parse_compare_response(json.dumps({"data": {"repository": "nope"}})) == {}


class TestChunkHeads:
    """A 100-PR repo is split so no single query blows the GraphQL complexity budget."""

    def test_a_small_set_is_one_chunk(self) -> None:
        assert chunk_heads({1: "a", 2: "b"}) == [{1: "a", 2: "b"}]

    def test_an_oversized_set_is_split_and_loses_nothing(self) -> None:
        heads = {number: f"branch-{number}" for number in range(BEHIND_CHUNK_SIZE * 2 + 3)}

        chunks = chunk_heads(heads)

        assert len(chunks) == 3
        assert [len(chunk) for chunk in chunks] == [BEHIND_CHUNK_SIZE, BEHIND_CHUNK_SIZE, 3]
        assert {number: ref for chunk in chunks for number, ref in chunk.items()} == heads

    def test_an_empty_set_issues_no_query(self) -> None:
        assert chunk_heads({}) == []
