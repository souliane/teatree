"""Shared Slack archive permalink contract."""

from teatree.utils.slack_links import slack_permalink


def test_slack_permalink_uses_archive_link_and_rejects_partial_coordinates() -> None:
    assert slack_permalink("C9", "1779.0001") == "https://slack.com/archives/C9/p17790001"
    assert slack_permalink("", "1779.0001") == ""
    assert slack_permalink("C9", "") == ""
