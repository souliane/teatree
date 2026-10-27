from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from unittest.mock import patch

import pytest

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
        patch("teatree.core.merge.execution.CodeHostQuery.for_ref", return_value=object()),
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
