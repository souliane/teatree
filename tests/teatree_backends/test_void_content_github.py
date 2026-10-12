import json

from teatree.backends.forge_merge_rpc import GhMergeRpc
from teatree.core.forge_content import ContentPresence


def _runner(*, same_blob: bool = False, truncated: bool = False, complete: bool = True):
    def run(argv: list[str]) -> tuple[int, str, str]:
        if argv[0:2] == ["pr", "view"]:
            return 0, json.dumps({"headRefOid": "a" * 40, "changedFiles": 1 if complete else 2}), ""
        endpoint = next((arg for arg in argv if arg.startswith("repos/")), "")
        if endpoint == "repos/acme/widget":
            body = {"default_branch": "main"}
        elif endpoint == "repos/acme/widget/commits/main":
            body = {"sha": "b" * 40, "commit": {"tree": {"sha": "d" * 40}}}
        elif endpoint == "repos/acme/widget/git/commits/" + "a" * 40:
            body = {"tree": {"sha": "c" * 40}}
        elif endpoint.endswith("/pulls/7/files"):
            return 0, "src/a.py\n", ""
        elif endpoint == "repos/acme/widget/git/trees/" + "c" * 40 + "?recursive=1":
            body = {"truncated": truncated, "tree": [{"path": "src/a.py", "type": "blob", "sha": "x" * 40}]}
        elif endpoint == "repos/acme/widget/git/trees/" + "d" * 40 + "?recursive=1":
            body = {
                "truncated": False,
                "tree": [{"path": "src/a.py", "type": "blob", "sha": ("x" if same_blob else "y") * 40}],
            }
        else:
            return 1, "", f"unexpected endpoint: {endpoint}"
        return 0, json.dumps(body), ""

    return run


def test_github_checks_live_default_and_changed_blob() -> None:
    evidence = GhMergeRpc(_runner()).fetch_pr_content_evidence(slug="acme/widget", pr_id=7)
    assert evidence.presence is ContentPresence.ABSENT
    assert evidence.checked_paths == ("src/a.py",)
    assert (
        GhMergeRpc(_runner(same_blob=True)).fetch_pr_content_evidence(slug="acme/widget", pr_id=7).presence
        is ContentPresence.PRESENT
    )


def test_github_truncated_tree_fails_closed() -> None:
    assert (
        GhMergeRpc(_runner(truncated=True)).fetch_pr_content_evidence(slug="acme/widget", pr_id=7).presence
        is ContentPresence.UNKNOWN
    )
    assert (
        GhMergeRpc(_runner(complete=False)).fetch_pr_content_evidence(slug="acme/widget", pr_id=7).presence
        is ContentPresence.UNKNOWN
    )
