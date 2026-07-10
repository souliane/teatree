"""Protocol-coverage fitness for the merge-RPC methods (PR 4 / #1985).

``runtime_checkable`` only checks method NAMES; these tests additionally pin the
exact keyword-only signature so a drifted parameter (or a removed method on one
host) is caught deterministically. Both concrete code hosts MUST implement the
full :class:`CodeHostBackend` merge-RPC surface — the argv lives here, so a host
that drops a method silently re-opens the host_kind transport branch in core.
"""

import inspect
from typing import TYPE_CHECKING, cast

from teatree.backends.github import GitHubCodeHost
from teatree.backends.gitlab import GitLabCodeHost
from teatree.core.backend_protocols import CodeHostBackend

if TYPE_CHECKING:
    from collections.abc import Callable

_MERGE_RPC_SIGNATURES: dict[str, list[tuple[str, inspect._ParameterKind]]] = {
    "fetch_live_head_sha": [
        ("slug", inspect.Parameter.KEYWORD_ONLY),
        ("pr_id", inspect.Parameter.KEYWORD_ONLY),
    ],
    "fetch_pr_merge_state": [
        ("slug", inspect.Parameter.KEYWORD_ONLY),
        ("pr_id", inspect.Parameter.KEYWORD_ONLY),
    ],
    "fetch_pr_is_draft": [
        ("slug", inspect.Parameter.KEYWORD_ONLY),
        ("pr_id", inspect.Parameter.KEYWORD_ONLY),
    ],
    "fetch_required_checks_rollup": [
        ("slug", inspect.Parameter.KEYWORD_ONLY),
        ("pr_id", inspect.Parameter.KEYWORD_ONLY),
    ],
    "fetch_required_status_check_contexts": [
        ("slug", inspect.Parameter.KEYWORD_ONLY),
        ("pr_id", inspect.Parameter.KEYWORD_ONLY),
    ],
    "fetch_pr_changed_paths": [
        ("slug", inspect.Parameter.KEYWORD_ONLY),
        ("pr_id", inspect.Parameter.KEYWORD_ONLY),
    ],
    "merge_pr_squash_bound": [
        ("slug", inspect.Parameter.KEYWORD_ONLY),
        ("pr_id", inspect.Parameter.KEYWORD_ONLY),
        ("expected_head_oid", inspect.Parameter.KEYWORD_ONLY),
    ],
}


def _params(method: object) -> list[tuple[str, inspect._ParameterKind]]:
    sig = inspect.signature(cast("Callable[..., object]", method))
    return [(name, p.kind) for name, p in sig.parameters.items() if name != "self"]


def _assert_host_implements_merge_rpc(host: object) -> None:
    for name, expected in _MERGE_RPC_SIGNATURES.items():
        method = getattr(host, name, None)
        assert method is not None, f"{type(host).__name__} is missing {name}"
        assert _params(method) == expected, f"{type(host).__name__}.{name} signature drifted"


def test_github_code_host_implements_every_merge_rpc_method() -> None:
    _assert_host_implements_merge_rpc(GitHubCodeHost(token="x"))


def test_gitlab_code_host_implements_every_merge_rpc_method() -> None:
    _assert_host_implements_merge_rpc(GitLabCodeHost(token="x", base_url="https://gitlab.com/api/v4"))


_WAVE2_READ_SIGNATURES: dict[str, list[tuple[str, inspect._ParameterKind]]] = {
    "list_prs": [
        ("repo", inspect.Parameter.KEYWORD_ONLY),
        ("state", inspect.Parameter.KEYWORD_ONLY),
        ("author", inspect.Parameter.KEYWORD_ONLY),
    ],
    "get_pr_diff": [
        ("repo", inspect.Parameter.KEYWORD_ONLY),
        ("pr_iid", inspect.Parameter.KEYWORD_ONLY),
    ],
    "list_pr_commits": [
        ("repo", inspect.Parameter.KEYWORD_ONLY),
        ("pr_iid", inspect.Parameter.KEYWORD_ONLY),
    ],
    "get_repo": [
        ("repo", inspect.Parameter.KEYWORD_ONLY),
    ],
}


def _assert_host_implements_wave2_reads(host: object) -> None:
    for name, expected in _WAVE2_READ_SIGNATURES.items():
        method = getattr(host, name, None)
        assert method is not None, f"{type(host).__name__} is missing {name}"
        assert _params(method) == expected, f"{type(host).__name__}.{name} signature drifted"


def test_github_code_host_implements_every_wave2_read() -> None:
    _assert_host_implements_wave2_reads(GitHubCodeHost(token="x"))


def test_gitlab_code_host_implements_every_wave2_read() -> None:
    _assert_host_implements_wave2_reads(GitLabCodeHost(token="x", base_url="https://gitlab.com/api/v4"))


_WAVE2_WRITE_SIGNATURES: dict[str, list[tuple[str, inspect._ParameterKind]]] = {
    "create_issue": [
        ("repo", inspect.Parameter.KEYWORD_ONLY),
        ("title", inspect.Parameter.KEYWORD_ONLY),
        ("body", inspect.Parameter.KEYWORD_ONLY),
        ("labels", inspect.Parameter.KEYWORD_ONLY),
    ],
    "post_issue_comment": [
        ("issue_url", inspect.Parameter.KEYWORD_ONLY),
        ("body", inspect.Parameter.KEYWORD_ONLY),
    ],
    "close_issue": [
        ("issue_url", inspect.Parameter.KEYWORD_ONLY),
        ("comment", inspect.Parameter.KEYWORD_ONLY),
    ],
    "update_issue": [
        ("issue_url", inspect.Parameter.KEYWORD_ONLY),
        ("body", inspect.Parameter.KEYWORD_ONLY),
    ],
}


def _assert_host_implements_issue_writes(host: object) -> None:
    for name, expected in _WAVE2_WRITE_SIGNATURES.items():
        method = getattr(host, name, None)
        assert method is not None, f"{type(host).__name__} is missing {name}"
        assert _params(method) == expected, f"{type(host).__name__}.{name} signature drifted"


def test_github_code_host_implements_every_issue_write() -> None:
    _assert_host_implements_issue_writes(GitHubCodeHost(token="x"))


def test_gitlab_code_host_implements_every_issue_write() -> None:
    _assert_host_implements_issue_writes(GitLabCodeHost(token="x", base_url="https://gitlab.com/api/v4"))


def test_both_hosts_are_runtime_code_host_backends() -> None:
    assert isinstance(GitHubCodeHost(token="x"), CodeHostBackend)
    assert isinstance(
        GitLabCodeHost(token="x", base_url="https://gitlab.com/api/v4"),
        CodeHostBackend,
    )
