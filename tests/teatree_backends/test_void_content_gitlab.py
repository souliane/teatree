from unittest.mock import Mock

from teatree.backends.gitlab.merge_rpc import GitLabApiMergeRpc
from teatree.core.forge_content import ContentPresence


def _client(*, same_blob: bool = False, complete: bool = True) -> Mock:
    client = Mock()
    client.get_json.side_effect = lambda endpoint: {
        "projects/acme%2Fwidget/merge_requests/7": {"sha": "a" * 40, "changes_count": "1" if complete else "2"},
        "projects/acme%2Fwidget": {"default_branch": "main"},
        "projects/acme%2Fwidget/repository/branches/main": {"commit": {"id": "b" * 40}},
    }[endpoint]
    client.get_json_paginated.side_effect = lambda endpoint: (
        [{"new_path": "src/a.py", "old_path": "src/a.py"}]
        if "/diffs?" in endpoint
        else [
            {
                "path": "src/a.py",
                "type": "blob",
                "id": ("x" if "ref=" + "a" * 40 in endpoint or same_blob else "y") * 40,
                "mode": "100644",
            }
        ]
    )
    return client


def test_gitlab_reads_default_and_changed_blobs() -> None:
    absent = GitLabApiMergeRpc(_client()).fetch_pr_content_evidence(slug="acme/widget", pr_id=7)
    present = GitLabApiMergeRpc(_client(same_blob=True)).fetch_pr_content_evidence(slug="acme/widget", pr_id=7)
    assert absent.presence is ContentPresence.ABSENT
    assert present.presence is ContentPresence.PRESENT


def test_gitlab_incomplete_diff_fails_closed() -> None:
    evidence = GitLabApiMergeRpc(_client(complete=False)).fetch_pr_content_evidence(slug="acme/widget", pr_id=7)
    assert evidence.presence is ContentPresence.UNKNOWN
