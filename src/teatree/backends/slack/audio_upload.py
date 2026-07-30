"""The #2050 attach-audio-to-a-DM upload orchestration, split out of ``bot.py``.

The modern three-step external upload (``files.upload`` is deprecated) —
``getUploadURLExternal`` reserves an off-Slack url + file id, the bytes are
POSTed there, ``completeUploadExternal`` shares the file into the channel as a
single DM (text + inline player) — factored into a free function so ``bot.py``
stays under the module-health LOC cap. The token is resolved by the caller (the
#1750 route) and passed in with the already-open :class:`SlackHttpClient`.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.backends.slack.http import SlackHttpClient
from teatree.backends.slack.upload_response import shared_message_ts
from teatree.types import RawAPIDict


@dataclass(frozen=True)
class AudioDmRequest:
    channel: str
    filepath: str
    text: str
    thread_ts: str = ""
    title: str = ""


def upload_audio_dm(*, http: SlackHttpClient, token: str, request: AudioDmRequest) -> RawAPIDict:
    """Share ``request.filepath`` into the channel as ONE DM: ``text`` + inline audio (#2050).

    ``getUploadURLExternal`` reserves an off-Slack ``upload_url`` + file ``id``;
    the bytes are POSTed there; ``completeUploadExternal`` shares the file into
    ``channel`` with ``text`` as the ``initial_comment`` and, when set,
    ``thread_ts`` — a SINGLE DM (text + inline player).

    Finalising requires the token's ``files:write`` scope; without it the reserve
    step returns ``ok:false`` / ``missing_scope`` (surfaced verbatim so the caller
    degrades to a text-only post). Returns the raw ``completeUploadExternal`` body
    (``{}`` when the file is unreadable).
    """
    path = Path(request.filepath)
    try:
        content = path.read_bytes()
    except OSError:
        return {}
    reserve = http.get(
        "files.getUploadURLExternal", token=token, params={"filename": path.name, "length": len(content)}
    )
    if not reserve.get("ok"):
        return reserve
    upload_url = reserve.get("upload_url")
    file_id = reserve.get("file_id")
    if not isinstance(upload_url, str) or not isinstance(file_id, str):
        return reserve
    http.post_external(upload_url, content=content)
    file_entry: RawAPIDict = {"id": file_id}
    if request.title:
        file_entry["title"] = request.title
    payload: RawAPIDict = {"files": [file_entry], "channel_id": request.channel, "initial_comment": request.text}
    if request.thread_ts:
        payload["thread_ts"] = request.thread_ts
    body = http.post("files.completeUploadExternal", token=token, json=payload, idempotent=False)
    if shared_ts := shared_message_ts(body, channel=request.channel):
        body["ts"] = shared_ts
    return body
