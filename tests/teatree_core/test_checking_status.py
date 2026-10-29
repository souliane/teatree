"""Owner-facing feature summary in the check-in report."""

import re
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.checking import CheckingReport, gather_checking_report
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.worker_generation import WorkerGeneration
from teatree.paths import teatree_source_root
from teatree.utils.git_run import run_strict
from tests._owner_channel import owner_card
from tests.factories import MergeAuditFactory, PullRequestFactory, TaskFactory, TicketFactory


class CheckInFeatureStatusTestCase(TestCase):
    def _report(self) -> CheckingReport:
        now = timezone.now() + timedelta(seconds=1)
        return gather_checking_report(
            since=now - timedelta(days=1), now=now, overlay_name="t3-teatree", code_host="github"
        )

    def test_report_opens_with_grouped_feature_status(self) -> None:
        first = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/101", short_description="message delivery"
        )
        second = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/102", short_description="dashboard access"
        )
        third = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/103", short_description="review workflow 5789 v2"
        )
        other = TicketFactory(issue_url="", short_description="routine maintenance")
        MergeAuditFactory(clear__ticket=first, clear__slug="example/widgets", clear__pr_id=201)
        MergeAuditFactory(clear__ticket=third, clear__slug="example/widgets", clear__pr_id=203)
        PullRequestFactory(
            ticket=first, repo="example/widgets", iid="202", url="https://github.com/example/widgets/pull/202"
        )
        PullRequestFactory(
            ticket=second,
            repo="example/widgets",
            iid="204",
            url="https://github.com/example/widgets/pull/204",
            state=PullRequest.State.REVIEW_REQUESTED,
        )
        PullRequestFactory(
            ticket=other, repo="example/widgets", iid="205", url="https://github.com/example/widgets/pull/205"
        )
        task = TaskFactory(ticket=second, session__ticket=second)
        DeferredQuestion.record(
            "Can I publish the dashboard fix?",
            parked_task=task,
            card=owner_card(),
            slack_channel="DQUESTION",
            slack_ts="1800000000.000001",
        )

        report = self._report()
        status = report.to_dict()["status_by_feature"]
        assert [feature["title"] for feature in status["features"]] == [
            "message delivery",
            "review workflow",
            "dashboard access",
            "Other maintenance",
        ]
        assert report.to_terse().startswith("Status by feature\n")
        assert "https://github.com/example/widgets/pull/203" in report.status_by_feature.render()[2]
        assert status["features"][0]["left"][0]["url"] == "https://github.com/example/widgets/pull/202"
        assert status["features"][2]["owner_waits"][0]["url"].startswith("https://slack.com/archives/")
        assert {item["url"] for item in status["not_yet_deployed"]} == {
            "https://github.com/example/widgets/pull/201",
            "https://github.com/example/widgets/pull/203",
        }
        for feature in status["features"]:
            assert not re.search(r"\d", feature["title"])
            for item in [*feature["merged"], *feature["left"], *feature["owner_waits"]]:
                assert item["url"]
                assert not re.search(r"\d", item["label"])
        visible = re.sub(r"\]\([^)]*\)", "]", "\n".join(report.status_by_feature.render()))
        assert not re.search(r"\d", visible)

    def test_live_completed_feature_has_no_link(self) -> None:
        sha = run_strict(repo=str(teatree_source_root()), args=["rev-parse", "HEAD"])
        WorkerGeneration.objects.create(sha=sha, image="test-image", state=WorkerGeneration.State.ACTIVE)
        WorkerGeneration.objects.create(
            sha="a" * 40,
            image="old-image",
            state=WorkerGeneration.State.STARTING,
            started_at=timezone.now() - timedelta(days=2),
        )
        ticket = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/301", short_description="finish access"
        )
        MergeAuditFactory(clear__ticket=ticket, clear__slug="example/widgets", clear__pr_id=302, merged_sha=sha)

        status = self._report().status_by_feature

        assert status.features[0].live is True
        assert status.not_yet_deployed == []
        assert status.deploy_state == "verified"
        assert "http" not in status.render()[1]

    def test_caps_features_and_omits_empty_section(self) -> None:
        assert self._report().status_by_feature.render() == []
        for number in range(9):
            ticket = TicketFactory(
                issue_url=f"https://github.com/example/widgets/issues/{400 + number}",
                short_description=f"feature {chr(65 + number)}",
            )
            PullRequestFactory(ticket=ticket)

        status = self._report().status_by_feature

        assert len(status.features) == 8
        assert status.more == 1
        assert status.render()[-1] == "…and 1 more features"

    def test_global_owner_question_is_under_other_maintenance(self) -> None:
        DeferredQuestion.record(
            "Can I publish the prepared change?",
            card=owner_card(),
            slack_channel="DQUESTION",
            slack_ts="1800000000.000001",
        )

        status = self._report().status_by_feature

        assert len(status.features) == 1
        assert status.features[0].title == "Other maintenance"
        assert status.features[0].owner_waits[0].url.startswith("https://slack.com/archives/")

    def test_unposted_owner_question_does_not_render_an_unlinked_label(self) -> None:
        ticket = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/401", short_description="publish workflow"
        )
        task = TaskFactory(ticket=ticket, session__ticket=ticket)
        DeferredQuestion.record("Can I publish the prepared change?", parked_task=task, card=owner_card())

        status = self._report().status_by_feature

        assert status.features[0].owner_waits == []
        assert status.to_dict()["features"][0]["pending_delivery"] == 1
        assert "waiting for Slack delivery" in status.render()[1]
        assert "Can I publish" not in status.render()[1]
