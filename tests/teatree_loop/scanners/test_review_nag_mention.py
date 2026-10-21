"""``reask_mention`` resolves the configured re-ask mention through a Slack directory read."""

import logging
from hashlib import sha256
from unittest.mock import patch

from django.core.cache import cache as django_cache
from django.test import TestCase

from teatree.core.models import ConfigSetting, ReviewRequestPost
from teatree.loop.scanners import review_nag_mention
from teatree.loop.scanners.review_nag_mention import RequesterIdentity, engaged_reask_mention, reask_mention
from tests.teatree_loop.test_review_nag_scanner import FakeHost, FakeSlack, _active_member


class TestReaskMention(TestCase):
    def _mention(
        self,
        slack: FakeSlack,
        host: FakeHost | None,
        *,
        requester: RequesterIdentity | None = None,
    ) -> str:
        post = ReviewRequestPost(
            mr_url="https://gitlab.example/x/-/merge_requests/1",
            slack_channel_id="CREVIEW",
            slack_thread_ts="1000",
        )
        return engaged_reask_mention(
            slack, host, [post], requester=requester or RequesterIdentity("UOWNER", ("owner",)), overlay_name=""
        )

    def test_system_note_author_is_not_an_engager(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "S0REVIEWERS")
        slack = FakeSlack(members=[_active_member("UPEER", "peer")])
        host = FakeHost(comments=[{"system": True, "author": {"username": "peer"}}])

        assert self._mention(slack, host) == "<!subteam^S0REVIEWERS>"

    def test_configured_fallback_does_not_cache_an_unmapped_reviewer(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "S0REVIEWERS")
        name = "cache-peer-with-fallback"
        cache_key = "review-nag-unmapped:" + sha256(f"\0{name}".encode()).hexdigest()
        django_cache.delete(cache_key)
        host = FakeHost(comments=[{"author": {"username": name}}])

        assert self._mention(FakeSlack(), host) == "<!subteam^S0REVIEWERS>"
        assert django_cache.get(cache_key) is None

    def test_unmapped_reviewer_cache_expires_after_thirty_minutes(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "")
        host = FakeHost(comments=[{"author": {"username": "cache-peer-no-fallback"}}])

        with patch.object(django_cache, "set", wraps=django_cache.set) as cache_set:
            assert self._mention(FakeSlack(), host) == ""

        assert cache_set.call_args.kwargs["timeout"] == 1800

    def test_configured_fallback_bypasses_previous_negative_cache(self) -> None:
        name = "cache-peer-recovered"
        host = FakeHost(comments=[{"author": {"username": name}}])
        ConfigSetting.objects.set_value("review_nag_reask_mention", "")
        assert self._mention(FakeSlack(), host) == ""
        ConfigSetting.objects.set_value("review_nag_reask_mention", "S0REVIEWERS")

        assert self._mention(FakeSlack(members=[_active_member("URECOVERED", name)]), host) == "<@URECOVERED>"

    def test_bot_replies_are_not_engagers(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "S0REVIEWERS")
        slack = FakeSlack(
            thread_replies=[
                {"ts": "1001", "bot_id": "B1", "user": "UPEER"},
                {"ts": "1002", "subtype": "bot_message", "user": "UPEER"},
            ]
        )

        assert self._mention(slack, FakeHost()) == "<!subteam^S0REVIEWERS>"

    def test_slackbot_reply_is_not_an_engager(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "S0REVIEWERS")
        slack = FakeSlack(thread_replies=[{"ts": "1001", "user": "USLACKBOT"}])

        assert self._mention(slack, FakeHost()) == "<!subteam^S0REVIEWERS>"

    def test_malformed_slack_id_is_not_mentioned(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "S0REVIEWERS")
        slack = FakeSlack(thread_replies=[{"ts": "1001", "user": "peer!"}])

        assert self._mention(slack, FakeHost()) == "<!subteam^S0REVIEWERS>"

    def test_requester_alias_is_removed_before_directory_lookup(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "S0REVIEWERS")
        slack = FakeSlack(members=[_active_member("UPEER", "alias")])
        host = FakeHost(comments=[{"author": {"username": "alias"}}])

        assert self._mention(slack, host, requester=RequesterIdentity("UOWNER", ("alias",))) == "<!subteam^S0REVIEWERS>"
        assert "users.list" not in slack.lookups

    def test_missing_host_uses_safe_group_fallback(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "S0REVIEWERS")

        assert self._mention(FakeSlack(), None) == "<!subteam^S0REVIEWERS>"

    def test_requester_user_fallback_is_held(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "owner")
        slack = FakeSlack(members=[{"id": "UOWNER", "name": "owner"}])

        assert self._mention(slack, None) == ""

    def test_engaged_reviewer_read_error_uses_group_fallback(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "S0REVIEWERS")
        host = FakeHost()

        with patch.object(host, "list_pr_comments", side_effect=RuntimeError("forge down")):
            assert self._mention(FakeSlack(), host) == "<!subteam^S0REVIEWERS>"

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
