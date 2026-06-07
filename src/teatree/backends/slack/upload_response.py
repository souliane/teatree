"""Parse the ``files.completeUploadExternal`` response (#2054).

``files.completeUploadExternal`` returns file objects, not the chat
message ``ts`` a ``chat.postMessage`` does. The shared message's ``ts``
lives under the first file's ``shares.private`` / ``shares.public`` entry
for the target channel. Resolving it here lets
:meth:`~teatree.backends.slack.bot.SlackBotBackend.post_audio_dm` keep the
same ``{"ok": ..., "ts": ...}`` evidence-pointer convention every text
post returns, so no caller has to special-case the audio-attach path.

Kept separate from the backend transport (mirrors ``scopes.py`` /
``token_policy.py``) so the response-shape knowledge has one home.
"""

from typing import cast

from teatree.types import RawAPIDict

_SHARE_VISIBILITIES = ("private", "public")


def shared_message_ts(body: RawAPIDict, *, channel: str) -> str:
    """The ``ts`` of the message ``completeUploadExternal`` shared into *channel*.

    Empty string when no ``shares`` entry for *channel* carries a ``ts``.
    """
    files = body.get("files")
    if not isinstance(files, list):
        return ""
    for raw_file in files:
        if not isinstance(raw_file, dict):
            continue
        shares = cast("RawAPIDict", raw_file).get("shares")
        if not isinstance(shares, dict):
            continue
        for visibility in _SHARE_VISIBILITIES:
            scope = cast("RawAPIDict", shares).get(visibility)
            if not isinstance(scope, dict):
                continue
            entries = cast("RawAPIDict", scope).get(channel)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                ts = cast("RawAPIDict", entry).get("ts") if isinstance(entry, dict) else None
                if isinstance(ts, str):
                    return ts
    return ""
