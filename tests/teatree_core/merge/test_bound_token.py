from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from unittest.mock import MagicMock, patch

import pytest

from teatree.core.merge import CodeHostQuery
from teatree.core.merge.bound_token import BoundTokenScopeError, bound_merge_token, bound_token_for
from teatree.core.merge.execution import execute_bound_merge
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.utils.pr_ref import PrRef


def test_bound_token_resets_on_success_and_exception() -> None:
    assert bound_token_for("group/repo") == ""
    with bound_merge_token("Group/Repo", "PERSON"):
        assert bound_token_for("group/repo") == "PERSON"
        with pytest.raises(BoundTokenScopeError):
            bound_token_for("other/repo")
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(bound_token_for, "group/repo").result() == ""
    assert bound_token_for("group/repo") == ""
    msg = "merge failed"
    with bound_merge_token("group/repo", "PERSON"), pytest.raises(ValueError, match=msg):
        raise ValueError(msg)
    assert bound_token_for("group/repo") == ""


def _token_seen_by_merge(ref: PrRef) -> str:
    resolution = ForgeTokenResolution("gitlab_token", "owner", ForgeTokenState.TOKEN, token="OWNER")
    observed: list[str] = []

    def stop_merge(**_kwargs: object) -> None:
        observed.append(bound_token_for(ref.slug))
        msg = "merge stopped"
        raise RuntimeError(msg)

    with (
        patch("teatree.forge_credentials.resolve_slug_token", return_value=resolution),
        patch("teatree.core.merge.execution.CodeHostQuery.for_ref", return_value=CodeHostQuery(ref, MagicMock())),
        patch("teatree.core.merge.execution.assert_merge_provenance_trusted", side_effect=stop_merge),
        pytest.raises(RuntimeError, match="merge stopped"),
    ):
        execute_bound_merge(ref=ref, expected_head_oid="abc")
    return observed[0]


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        pytest.param(None, "OWNER", id="no-scope-binds-the-owner-token"),
        pytest.param(("Group/Repo", "PERSON"), "PERSON", id="caller-scope-for-the-slug-is-kept"),
        pytest.param(("other/repo", "PERSON"), "OWNER", id="scope-for-another-slug-binds-the-owner-token"),
    ],
)
def test_execute_bound_merge_token_scope(scope: tuple[str, str] | None, expected: str) -> None:
    ref = PrRef(slug="group/repo", pr_id=7, host_kind="gitlab")
    with bound_merge_token(*scope) if scope else nullcontext():
        assert _token_seen_by_merge(ref) == expected
    assert bound_token_for(ref.slug) == ""


@contextmanager
def _stubbed_forge() -> Generator[MagicMock]:
    provider = MagicMock()
    owner = ForgeTokenResolution("gitlab_token", "owner", ForgeTokenState.TOKEN, token="OWNER")
    with (
        patch("teatree.core.merge.ci_rollup.get_backend_provider", return_value=provider),
        patch("teatree.core.merge.ci_rollup.resolve_slug_token", return_value=owner),
    ):
        yield provider


@pytest.mark.parametrize("host_kind", ["gitlab", "github"])
def test_query_built_inside_a_bound_scope_uses_the_bound_token(host_kind: str) -> None:
    ref = PrRef(slug="group/repo", pr_id=7, host_kind=host_kind)
    with _stubbed_forge() as provider, bound_merge_token("Group/Repo", "PERSON"):
        CodeHostQuery.for_ref(ref)
    build = provider.build_gitlab_host if host_kind == "gitlab" else provider.build_github_host
    assert build.call_args.kwargs["token"] == "PERSON"


def test_query_for_another_slug_inside_a_bound_scope_is_refused_before_a_host_is_built() -> None:
    ref = PrRef(slug="other/repo", pr_id=7, host_kind="gitlab")
    with _stubbed_forge() as provider, bound_merge_token("group/repo", "PERSON"), pytest.raises(BoundTokenScopeError):
        CodeHostQuery.for_ref(ref)
    provider.build_gitlab_host.assert_not_called()


def test_query_outside_a_bound_scope_keeps_the_owner_route_and_the_scope_does_not_leak() -> None:
    ref = PrRef(slug="group/repo", pr_id=7, host_kind="gitlab")
    with _stubbed_forge() as provider:
        CodeHostQuery.for_ref(ref)
        with bound_merge_token("group/repo", "PERSON"):
            CodeHostQuery.for_ref(ref)
        CodeHostQuery.for_ref(ref)
    tokens = [call.kwargs["token"] for call in provider.build_gitlab_host.call_args_list]
    assert tokens == ["OWNER", "PERSON", "OWNER"]
