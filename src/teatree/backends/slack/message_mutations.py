"""Routed Slack message edits and deletes."""

from collections.abc import Callable
from functools import partial

from teatree.backends.slack.dm_history import read_single_message, read_thread_replies
from teatree.slack_mrkdwn import wrap_slack_message
from teatree.types import RawAPIDict

type Getter = Callable[[str, dict[str, str | int]], RawAPIDict]
type TokenGetter = Callable[..., RawAPIDict]
type Poster = Callable[..., RawAPIDict]
type StripAudio = Callable[[list[RawAPIDict]], list[RawAPIDict]]


def read_message(get: Getter, strip_audio: StripAudio, *, channel: str, ts: str) -> RawAPIDict:
    """Preserve the legacy empty-on-refusal read and strip only the bot's own TTS audio."""
    message = read_single_message(get=get, channel=channel, ts=ts)
    return strip_audio([message])[0] if message else {}


def read_for_write(get: TokenGetter, *, token: str, channel: str, ts: str, thread_ts: str = "") -> RawAPIDict:
    routed_get = partial(get, token=token)
    if thread_ts:
        replies = read_thread_replies(get=routed_get, channel=channel, thread_ts=thread_ts, strict_complete=True)
        return next((reply for reply in replies if str(reply.get("ts", "")) == ts), {})
    return read_single_message(get=routed_get, channel=channel, ts=ts, refuse=True)


def update_routed(poster: Poster, *, token: str, channel: str, ts: str, text: str) -> RawAPIDict:
    if not token:
        return {}
    payload = {"channel": channel, "ts": ts, "text": wrap_slack_message(text), "blocks": []}
    return poster("chat.update", payload, token=token)


def delete_routed(poster: Poster, *, token: str, channel: str, ts: str) -> RawAPIDict:
    if not token:
        return {}
    return poster("chat.delete", {"channel": channel, "ts": ts}, token=token, idempotent=False)
