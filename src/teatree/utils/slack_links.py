"""Slack archive links shared across component boundaries."""


def slack_permalink(channel_id: str, thread_ts: str) -> str:
    if not channel_id or not thread_ts:
        return ""
    return f"https://slack.com/archives/{channel_id}/p{thread_ts.replace('.', '')}"
