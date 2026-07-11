"""Single-call Slack Web API ops, split out of ``SlackBotBackend``.

The ``auth.test`` scope probe, the permalink read, and the channel self-join —
each a one-shot Web API call returning a scalar/body — factored into free
functions taking the backend's http client or ``get`` / ``post`` callable so
``bot.py`` stays under the module-health LOC cap. Each caller keeps its own
empty-credential guard, so these assume a usable token/channel.
"""

from typing import Protocol

from teatree.backends.slack.http import SlackHttpClient
from teatree.backends.slack.scopes import OAUTH_SCOPES_HEADER, attach_granted_scopes
from teatree.types import RawAPIDict


class Getter(Protocol):
    def __call__(self, method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict: ...


class Poster(Protocol):
    def __call__(self, method: str, payload: RawAPIDict, *, token: str = "", idempotent: bool = True) -> RawAPIDict: ...


def run_auth_test(http: SlackHttpClient, bot_token: str) -> RawAPIDict:
    """Return the ``auth.test`` body with granted scopes attached from ``X-OAuth-Scopes``.

    Slack reports the token's scopes in the response header, not the JSON body;
    they are attached under :data:`GRANTED_SCOPES_KEY` (native keys untouched) so a
    connector-preflight scope guard can read them.
    """
    body, scopes_header = http.post_with_header("auth.test", token=bot_token, json={}, header=OAUTH_SCOPES_HEADER)
    return attach_granted_scopes(body, scopes_header)


def read_permalink(get: Getter, channel: str, ts: str) -> str:
    """Return the archive permalink for ``(channel, ts)`` or ``""``."""
    if not channel or not ts:
        return ""
    data = get("chat.getPermalink", {"channel": channel, "message_ts": ts})
    if not data.get("ok"):
        return ""
    permalink = data.get("permalink", "")
    return permalink if isinstance(permalink, str) else ""


def join_conversation(post: Poster, channel: str) -> RawAPIDict:
    """Join the bot to a public channel via ``conversations.join`` (bot token).

    Returns the raw Slack body. ``ok:true`` is returned both on a fresh join and
    when the bot is already a member (Slack sets ``already_in_channel``), so callers
    treat the call as idempotent. A private or Slack-Connect channel rejects a
    self-join with an error in the body.
    """
    if not channel:
        return {}
    return post("conversations.join", {"channel": channel})
