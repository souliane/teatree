"""``build_pr_entry`` reports the pipeline of the MR's head commit, never the MR's newest pipeline."""

from django.test import TestCase

from teatree.backends.gitlab.api import ProjectInfo
from teatree.backends.gitlab.sync_prs import _PRContext, build_pr_entry
from tests.teatree_backends._gitlab_wire import GitLabWire

_HEAD, _OLDER, _TRAIN = "a" * 40, "b" * 40, "c" * 40
_MR = "projects/123/merge_requests/42"


def _entry_for(pipelines: list[dict[str, object]]):
    wire = GitLabWire(
        {
            f"{_MR}/pipelines": pipelines,
            f"{_MR}/approvals": {"approved_by": [], "approvals_required": 1},
            f"{_MR}/discussions": [],
            f"{_MR}/draft_notes": [],
        }
    )
    raw = {
        "web_url": "https://gitlab.example/org/repo/-/merge_requests/42",
        "iid": 42,
        "project_id": 123,
        "sha": _HEAD,
        "draft": False,
    }
    project = ProjectInfo(project_id=123, path_with_namespace="org/repo", short_name="repo")
    return build_pr_entry(_PRContext(raw=raw, repo_short="repo", client=wire, project=project), username="alice")


class TestSyncedPipelineIsTheHeadCommits(TestCase):
    def test_an_older_commits_green_is_not_reported_for_a_head_without_a_pipeline(self) -> None:
        entry = _entry_for([{"status": "success", "sha": _OLDER, "source": "push", "web_url": "https://p/1"}])

        assert (entry.pipeline_status, entry.pipeline_url) == (None, None)

    def test_the_head_pipeline_is_reported_behind_a_newer_merge_train_pipeline(self) -> None:
        entry = _entry_for(
            [
                {"status": "canceled", "sha": _TRAIN, "source": "merge_train", "web_url": "https://p/3"},
                {"status": "success", "sha": _HEAD, "source": "merge_request_event", "web_url": "https://p/2"},
            ]
        )

        assert (entry.pipeline_status, entry.pipeline_url) == ("success", "https://p/2")
