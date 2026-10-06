"""``reask_mention`` resolves the configured re-ask mention through a Slack directory read."""

import logging

from django.test import TestCase

from teatree.core.models import ConfigSetting
from teatree.loop.scanners import review_nag_mention
from teatree.loop.scanners.review_nag_mention import reask_mention
from tests.teatree_loop.test_review_nag_scanner import FakeSlack


class TestReaskMention(TestCase):
    def test_a_handle_matching_only_a_user_renders_a_user_mention(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "reviewers-team")
        slack = FakeSlack(members=[{"id": "U0REVIEWER", "name": "reviewers-team"}])

        mention = reask_mention(slack, overlay_name="")

        assert mention == "<@U0REVIEWER>"
        assert "subteam" not in mention

    def test_an_unresolvable_handle_warns_once_and_mentions_nobody(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "reviewers-team")
        slack = FakeSlack()

        with self.assertLogs(review_nag_mention.logger, level=logging.WARNING) as logs:
            mentions = [reask_mention(slack, overlay_name="") for _ in range(2)]

        assert mentions == ["", ""]
        assert len([line for line in logs.output if "reviewers-team" in line]) == 1

    def test_a_lookup_that_raises_mentions_nobody(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "reviewers-team")
        slack = FakeSlack(raise_on_resolve=RuntimeError("api down"))

        assert reask_mention(slack, overlay_name="") == ""
