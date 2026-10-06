"""``reask_mention`` resolves the configured re-ask mention through a Slack directory read."""

import logging
from dataclasses import dataclass, field

from django.test import TestCase

from teatree.backends.slack.web_reads import resolve_user_id, resolve_usergroup_id
from teatree.core.models import ConfigSetting
from teatree.loop.scanners import review_nag_mention
from teatree.loop.scanners.review_nag_mention import reask_mention
from teatree.types import RawAPIDict


@dataclass
class _SlackDirectory:
    usergroups: list[RawAPIDict] = field(default_factory=list)
    members: list[RawAPIDict] = field(default_factory=list)
    lookups: list[str] = field(default_factory=list)
    raise_on_lookup: Exception | None = None

    def _get(self, method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
        _ = (params, token)
        self.lookups.append(method)
        if self.raise_on_lookup is not None:
            raise self.raise_on_lookup
        if method == "usergroups.list":
            return {"ok": True, "usergroups": list(self.usergroups)}
        return {"ok": True, "members": list(self.members)}

    def resolve_usergroup_id(self, handle: str) -> str:
        return resolve_usergroup_id(get=self._get, handle=handle)

    def resolve_user_id(self, handle: str) -> str:
        return resolve_user_id(get=self._get, handle=handle)


class TestReaskMention(TestCase):
    def test_a_group_id_renders_a_group_mention_without_a_lookup(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "S0REVIEWERS")
        directory = _SlackDirectory()

        assert reask_mention(directory, overlay_name="") == "<!subteam^S0REVIEWERS>"
        assert directory.lookups == []

    def test_a_leading_at_sign_on_a_handle_is_tolerated(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "@reviewers-team")
        directory = _SlackDirectory(usergroups=[{"id": "S0REVIEWERS", "handle": "reviewers-team"}])

        assert reask_mention(directory, overlay_name="") == "<!subteam^S0REVIEWERS>"

    def test_a_handle_matching_only_a_user_renders_a_user_mention(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "reviewers-team")
        directory = _SlackDirectory(members=[{"id": "U0REVIEWER", "name": "reviewers-team"}])

        mention = reask_mention(directory, overlay_name="")

        assert mention == "<@U0REVIEWER>"
        assert "subteam" not in mention

    def test_an_unresolvable_handle_warns_once_and_mentions_nobody(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "reviewers-team")
        directory = _SlackDirectory()

        with self.assertLogs(review_nag_mention.logger, level=logging.WARNING) as logs:
            mentions = [reask_mention(directory, overlay_name="") for _ in range(2)]

        assert mentions == ["", ""]
        assert len([line for line in logs.output if "reviewers-team" in line]) == 1

    def test_a_lookup_that_raises_mentions_nobody(self) -> None:
        ConfigSetting.objects.set_value("review_nag_reask_mention", "reviewers-team")
        directory = _SlackDirectory(raise_on_lookup=RuntimeError("api down"))

        assert reask_mention(directory, overlay_name="") == ""
