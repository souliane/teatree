"""Owner-facing feature summary in the check-in report."""

import re
import subprocess
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.checking import CheckingReport, gather_checking_report
from teatree.core.checking_report.status import plain_title
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.ticket import Ticket
from teatree.core.models.worker_generation import WorkerGeneration
from teatree.core.review.pr_inventory import PrInventoryEntry, write_inventory
from teatree.core.review.pr_next_action import UPDATE_BRANCH
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
            "dashboard access",
            "review workflow",
            "message delivery",
            "Other maintenance",
        ]
        assert report.to_terse().startswith("Status by feature\n")
        assert "https://github.com/example/widgets/pull/203" in report.status_by_feature.render()[2]
        assert status["features"][2]["left"][0]["url"] == "https://github.com/example/widgets/pull/202"
        assert status["features"][0]["owner_waits"][0]["url"].startswith("https://slack.com/archives/")
        assert status["not_yet_deployed"] == []
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
            issue_url="https://github.com/souliane/teatree/issues/301", short_description="finish access"
        )
        MergeAuditFactory(clear__ticket=ticket, clear__slug="souliane/teatree", clear__pr_id=302, merged_sha=sha)

        status = self._report().status_by_feature

        assert status.features[0].live is True
        assert status.not_yet_deployed == []
        assert status.deploy_state == "verified"
        assert "http" not in status.render()[1]

    def test_partly_live_feature_lists_only_the_undeployed_merge(self) -> None:
        repo = str(teatree_source_root())
        serving_sha = run_strict(repo=repo, args=["rev-parse", "HEAD"])
        future_sha = "a" * 40
        WorkerGeneration.objects.create(sha=serving_sha, image="test-image", state=WorkerGeneration.State.ACTIVE)
        ticket = TicketFactory(
            issue_url="https://github.com/souliane/teatree/issues/401", short_description="message delivery"
        )
        MergeAuditFactory(
            clear__ticket=ticket, clear__slug="souliane/teatree", clear__pr_id=402, merged_sha=serving_sha
        )
        MergeAuditFactory(clear__ticket=ticket, clear__slug="souliane/teatree", clear__pr_id=403, merged_sha=future_sha)

        status = self._report().status_by_feature

        assert status.features[0].live == "partly"
        assert [item.url for item in status.not_yet_deployed] == ["https://github.com/souliane/teatree/pull/403"]

    def test_git_timeout_degrades_deployment_to_unknown(self) -> None:
        ticket = TicketFactory(issue_url="https://github.com/souliane/teatree/issues/410")
        MergeAuditFactory(clear__ticket=ticket, clear__slug="souliane/teatree", merged_sha="a" * 40)
        WorkerGeneration.objects.create(sha="b" * 40, image="test-image", state=WorkerGeneration.State.ACTIVE)

        with patch(
            "teatree.core.checking_report.status.run_with_status", side_effect=subprocess.TimeoutExpired("git", 3)
        ):
            status = self._report().status_by_feature

        assert status.deploy_state == "unknown"

    def test_other_repository_merge_does_not_change_worker_deployment_state(self) -> None:
        sha = run_strict(repo=str(teatree_source_root()), args=["rev-parse", "HEAD"])
        WorkerGeneration.objects.create(sha=sha, image="test-image", state=WorkerGeneration.State.ACTIVE)
        own = TicketFactory(issue_url="https://github.com/souliane/teatree/issues/411")
        other = TicketFactory(issue_url="https://github.com/example/widgets/issues/412")
        MergeAuditFactory(clear__ticket=own, clear__slug="souliane/teatree", merged_sha=sha)
        MergeAuditFactory(clear__ticket=other, clear__slug="example/widgets", merged_sha="a" * 40)

        status = self._report().status_by_feature

        assert status.deploy_state == "verified"
        assert status.not_yet_deployed == []

    def test_only_active_author_pull_requests_are_left(self) -> None:
        active = TicketFactory(issue_url="https://github.com/example/widgets/issues/420")
        PullRequestFactory(ticket=active, url="https://github.com/example/widgets/pull/421")
        for state in (Ticket.State.DELIVERED, Ticket.State.REVIEW_DELIVERED, Ticket.State.IGNORED):
            ticket = TicketFactory(state=state)
            PullRequestFactory(ticket=ticket)
        reviewer = TicketFactory(role=Ticket.Role.REVIEWER)
        PullRequestFactory(ticket=reviewer)

        status = self._report().status_by_feature

        assert [item.url for block in status.features for item in block.left] == [
            "https://github.com/example/widgets/pull/421"
        ]

    def test_failed_pipeline_is_not_described_as_pending(self) -> None:
        ticket = TicketFactory(
            extra={"prs": {"https://github.com/example/widgets/pull/430": {"pipeline_status": "failed"}}}
        )
        PullRequestFactory(ticket=ticket, url="https://github.com/example/widgets/pull/430")

        status = self._report().status_by_feature

        assert status.features[0].left[0].state == "tests failed"

    def test_fresh_pr_inventory_supplies_github_ci_and_branch_state(self) -> None:
        red_url = "https://github.com/example/widgets/pull/440"
        behind_url = "https://github.com/example/widgets/pull/441"
        pending_url = "https://github.com/example/widgets/pull/442"
        for url in (red_url, behind_url, pending_url):
            ticket = TicketFactory(issue_url=f"https://github.com/example/widgets/issues/{url.rsplit('/', 1)[1]}")
            PullRequestFactory(ticket=ticket, url=url)
        cached_url = "https://github.com/example/widgets/pull/443"
        cached = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/443",
            extra={"prs": {cached_url: {"pipeline_status": "failed"}}},
        )
        PullRequestFactory(ticket=cached, url=cached_url)

        def entry(url: str, *, ci: str, decision: str, next_task: str) -> PrInventoryEntry:
            return PrInventoryEntry(
                url=url,
                repo="example/widgets",
                iid=int(url.rsplit("/", 1)[1]),
                title="repair checks",
                created_at="",
                updated_at="",
                next_task=next_task,
                decision=decision,
                ci=ci,
                approvals="unknown",
                threads="unknown",
                draft=False,
            )

        with (
            TemporaryDirectory() as directory,
            patch("teatree.core.review.pr_inventory.data_dir_root", return_value=Path(directory)),
        ):
            write_inventory(
                "t3-teatree",
                [
                    entry(red_url, ci="red", decision="my_pr.failed", next_task="Fix failing CI"),
                    entry(behind_url, ci="green", decision="needs_branch_update", next_task=UPDATE_BRANCH),
                    entry(pending_url, ci="pending", decision="my_pr.open", next_task="Inspect CI"),
                    entry(cached_url, ci="unknown", decision="my_pr.open", next_task="Inspect CI"),
                ],
            )
            status = self._report().status_by_feature

        states = {item.url: item.state for block in status.features for item in block.left}
        assert states[red_url] == "tests failed"
        assert states[behind_url] == "needs a merge of main"
        assert states[pending_url] == "waiting for the tests"
        assert states[cached_url] == "tests failed"

    def test_technical_words_survive_number_removal(self) -> None:
        assert plain_title("Fix S3 bucket policy") == "Fix S3 bucket policy"
        assert plain_title("IPv6 routing") == "IPv6 routing"

    def test_red_pull_request_survives_feature_cap(self) -> None:
        for number in range(8):
            ticket = TicketFactory(
                issue_url=f"https://github.com/example/widgets/issues/{100 + number}",
                short_description=f"routine feature {chr(65 + number)}",
            )
            PullRequestFactory(ticket=ticket)
        red_url = "https://github.com/example/widgets/pull/909"
        red = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/900",
            short_description="repair failing checks",
            extra={"prs": {red_url: {"pipeline_status": "failed"}}},
        )
        PullRequestFactory(ticket=red, url=red_url)

        status = self._report().status_by_feature

        assert "repair failing checks" in [block.title for block in status.features]
        assert status.more == 1

    def test_undeployed_merge_survives_checkpoint_window(self) -> None:
        serving_sha = run_strict(repo=str(teatree_source_root()), args=["rev-parse", "HEAD"])
        WorkerGeneration.objects.create(sha=serving_sha, image="test-image", state=WorkerGeneration.State.ACTIVE)
        ticket = TicketFactory(
            issue_url="https://github.com/souliane/teatree/issues/910",
            short_description="delayed delivery",
            state=Ticket.State.MERGED,
        )
        audit = MergeAuditFactory(
            clear__ticket=ticket, clear__slug="souliane/teatree", clear__pr_id=911, merged_sha="a" * 40
        )
        audit.merged_at = timezone.now() - timedelta(days=2)
        audit.save(update_fields=["merged_at"])

        status = self._report().status_by_feature

        assert [item.url for item in status.not_yet_deployed] == ["https://github.com/souliane/teatree/pull/911"]
        assert status.features[0].title == "delayed delivery"

    def test_related_features_share_a_named_lane_and_keep_problem_links(self) -> None:
        first = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/501",
            short_description="webhook delivery",
            extra={"labels": ["lane:webhooks"]},
        )
        second = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/502",
            short_description="webhook recovery",
            extra={"labels": ["lane:webhooks"]},
        )
        PullRequestFactory(ticket=first, url="https://github.com/example/widgets/pull/503")
        PullRequestFactory(ticket=second, url="https://github.com/example/widgets/pull/504")

        status = self._report().status_by_feature

        assert [feature.title for feature in status.features] == ["Webhooks"]
        assert len(status.features[0].left) == 2
        assert "https://github.com/example/widgets/pull/503" in status.render()[1]
        assert "https://github.com/example/widgets/pull/504" in status.render()[1]

    def test_owner_named_lanes_are_inferred_when_labels_are_absent(self) -> None:
        first = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/511",
            short_description="webhook delivery",
        )
        second = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/512",
            short_description="webhook recovery",
        )
        PullRequestFactory(ticket=first, url="https://github.com/example/widgets/pull/513")
        PullRequestFactory(ticket=second, url="https://github.com/example/widgets/pull/514")
        for index, title in enumerate(
            ("multi-user access", "agent communication on Slack", "coordinator check-in", "evals harness"),
            start=515,
        ):
            ticket = TicketFactory(
                issue_url=f"https://github.com/example/widgets/issues/{index}",
                short_description=title,
            )
            PullRequestFactory(ticket=ticket, url=f"https://github.com/example/widgets/pull/{index}")

        status = self._report().status_by_feature

        assert {feature.title for feature in status.features} == {
            "Webhooks",
            "Multi-user",
            "Agent communication",
            "Coordinator",
            "Evals",
        }
        assert len(next(feature for feature in status.features if feature.title == "Webhooks").left) == 2

    def test_explicit_and_inferred_names_share_the_same_lane(self) -> None:
        labelled = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/521",
            short_description="account access",
            extra={"labels": ["lane:multi-user"]},
        )
        inferred = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/522",
            short_description="multi-user roles",
        )
        PullRequestFactory(ticket=labelled, url="https://github.com/example/widgets/pull/523")
        PullRequestFactory(ticket=inferred, url="https://github.com/example/widgets/pull/524")

        status = self._report().status_by_feature

        assert [feature.title for feature in status.features] == ["Multi-user"]
        assert len(status.features[0].left) == 2

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

    def test_owner_question_survives_feature_cap(self) -> None:
        for number in range(8):
            ticket = TicketFactory(issue_url=f"https://github.com/example/widgets/issues/{600 + number}")
            MergeAuditFactory(clear__ticket=ticket, clear__slug="example/widgets")
        owner = TicketFactory(issue_url="https://github.com/example/widgets/issues/699")
        task = TaskFactory(ticket=owner, session__ticket=owner)
        DeferredQuestion.record(
            "Can I publish it?",
            parked_task=task,
            card=owner_card(),
            slack_channel="DQUESTION",
            slack_ts="1800000000.000001",
        )

        status = self._report().status_by_feature

        assert status.features[0].owner_waits
        assert status.more == 1

    def test_hidden_links_are_counted_in_visible_block(self) -> None:
        ticket = TicketFactory(issue_url="https://github.com/example/widgets/issues/700")
        for number in range(4):
            PullRequestFactory(ticket=ticket, url=f"https://github.com/example/widgets/pull/{710 + number}")

        line = self._report().status_by_feature.render()[1]

        assert "and 2 more" in line

    def test_incidental_lane_word_does_not_reclassify_feature(self) -> None:
        ticket = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/730",
            short_description="Repair an unrelated coordinator mention",
        )
        PullRequestFactory(ticket=ticket)

        status = self._report().status_by_feature

        assert status.features[0].title == "Repair an unrelated coordinator mention"

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

    def test_active_feature_without_pull_request_still_names_the_next_step(self) -> None:
        ticket = TicketFactory(
            issue_url="https://github.com/example/widgets/issues/601",
            short_description="agent communication",
            state=Ticket.State.CODED,
        )

        status = self._report().status_by_feature

        assert [feature.title for feature in status.features] == ["Agent communication"]
        assert status.features[0].left[0].url == ticket.issue_url
        assert status.features[0].left[0].state == "waiting for the tests"

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
