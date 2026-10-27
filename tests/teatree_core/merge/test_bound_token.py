from concurrent.futures import ThreadPoolExecutor

import pytest

from teatree.core.merge.bound_token import BoundTokenScopeError, bound_merge_token, bound_token_for


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
