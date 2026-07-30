"""Slack transport for the AskUserQuestion mirror (extracted from hook_router).

The PreToolUse mirror posts the user's ``AskUserQuestion`` to their Slack DM
so they see it on their phone before they answer. The transport — open the
DM, post the message, cache the channel id, format the question text — was a
self-contained ~250-LOC cluster inside ``hook_router`` that talked to the
Slack Web API over RAW ``urllib``. This leaf lifts the whole concern out and
converges it onto the hardened :class:`~teatree.backends.slack.http.SlackHttpClient`
(#1110: ``httpx`` + idempotency-aware bounded retry) that the rest of teatree
already uses — so the router shrinks by the whole concern.

A platform leaf, not a back-edge. ``teatree.hooks`` sits BELOW ``teatree.core``
and ``teatree.backends.slack`` in the layer DAG (tach), so this leaf must not
import either — not even lazily (tach scans deferred imports too). The
higher-layer capabilities it needs — the Slack ``post`` (an instance method of
``SlackHttpClient``, ``teatree.backends.slack``) and the active-DM-thread
lookup (``IncomingEvent``, ``teatree.core``) — are INJECTED by the caller as
:data:`Poster` / :data:`ThreadResolver` callables. The router (``hook_router``,
outside ``src`` and free to touch the domain) builds the concrete
implementations and passes them in. The leaf itself imports only stdlib, so it
stays a true leaf and never reaches ``hook_router`` either — pinned by
``tests/teatree_quality/test_hooks_import_direction.py``.

The transport keeps the router's timing contract. The mirror runs
synchronously inside the PreToolUse hook so the DM lands before the in-client
prompt renders, under a tight (~5s) hook timeout. So the injected
``SlackHttpClient`` is built with the router's short per-call timeout and NO
retry (``max_retries=0``) — a retry-with-backoff could blow the hook budget.
The reliability win is the shared hardened transport (``httpx``, correct
``ok``/``ts`` handling, idempotency: ``conversations.open`` is a read,
``chat.postMessage`` is not), not added retries. Every call still degrades to
``""`` so a Slack outage never blocks the question.
"""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from teatree.utils.run import run_allowed_to_fail


class Poster(Protocol):
    """The Slack ``post`` capability the transport needs, structurally typed.

    ``SlackHttpClient.post`` satisfies this. Typing it as a Protocol — not the
    concrete class — keeps this platform leaf from importing the higher-layer
    ``teatree.backends.slack`` module.
    """

    def __call__(self, method: str, *, token: str, json: dict, idempotent: bool) -> dict: ...


type ThreadResolver = Callable[[str], str]


# Audio enrichment for the mirrored question (#2171 TTS parity). Called
# ``(channel, text, thread_ts)`` AFTER the text question DM lands, so the
# spoken rendition reaches the user's phone the same way ``notify_user`` DMs
# already do. INJECTED by the router (which resolves the Slack backend and
# wraps ``teatree.core.speak.deliver_user_dm_sidecar``) so this leaf never
# imports ``teatree.core`` — it stays a pure platform leaf. Best-effort by
# contract: it must never raise into the post path, and the enrichment is
# skipped entirely when ``None`` (``speak.slack`` off — today's text-only mirror).
type AudioEnricher = Callable[[str, str, str], None]


def slack_config_from_registry() -> tuple[str, str] | None:
    """Return ``(bot_token_ref, user_id)`` from the first slack-enabled overlay.

    Sources the per-overlay Slack wiring from the DB overlays registry. Named for
    the registry it actually reads -- the legacy ``_from_toml`` name predated the
    move off a TOML file to the DB overlays registry and misdescribed the source
    (#F7.9).
    """
    from teatree.config import load_config  # noqa: PLC0415 — deferred: call-time import, kept lazy

    try:
        overlays = load_config().raw.get("overlays") or {}
    except Exception:  # noqa: BLE001 — config read is best-effort; a failure degrades to no mirror
        return None
    for overlay_cfg in overlays.values():
        if not isinstance(overlay_cfg, dict):
            continue
        if overlay_cfg.get("messaging_backend") == "slack":
            ref = overlay_cfg.get("slack_token_ref", "")
            uid = overlay_cfg.get("slack_user_id", "")
            if ref and uid:
                return ref, uid
    return None


def format_question_text(questions: list[dict]) -> str:
    """Render the AskUserQuestion payload for the Slack DM, tolerant of loose shapes.

    The harness input is opaque, not the typed view: a question may not be a
    mapping, ``options`` may be absent or not a list, and an option may be a bare
    string rather than a ``{label, description}`` mapping. Each shape is guarded
    so a ``.get`` on a non-mapping can NEVER raise into the never-raise mirror
    (an ``AttributeError`` here means the DM never lands).
    """
    lines: list[str] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        lines.append(f"*{q.get('question', '')}*")
        options = q.get("options", [])
        for i, opt in enumerate(options if isinstance(options, list) else [], 1):
            if isinstance(opt, dict):
                label = opt.get("label", "")
                desc = opt.get("description", "")
            elif isinstance(opt, str):
                label, desc = opt, ""
            else:
                continue
            lines.append(f"  {i}. {label}" + (f" — {desc}" if desc else ""))
    lines.append("\n_Reply with the number (e.g. `1`) or type your answer._")
    return "\n".join(lines)


def slack_dm_cache_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "teatree" / "slack_dm_channels.json"


def read_dm_channel_cache(user_id: str) -> str:
    path = slack_dm_cache_path()
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    cached = data.get(user_id)
    return cached if isinstance(cached, str) else ""


def write_dm_channel_cache(user_id: str, channel: str) -> None:
    """Persist the DM channel id (best-effort, never raises into the mirror).

    Both the read-merge and the mkdir/write are guarded: the cache lives under a
    dir that may be unwritable in the restricted hook subprocess, and an
    ``OSError`` from ``mkdir``/``write_text`` must never propagate into the
    PreToolUse mirror (a raise there means the question DM never lands).
    """
    path = slack_dm_cache_path()
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing[user_id] = channel
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        return


def _str_field(mapping: dict | None, key: str) -> str:
    """Return ``mapping[key]`` when it is a str, else ``""`` (``None`` mapping → ``""``)."""
    if mapping is None:
        return ""
    value = mapping.get(key)
    return value if isinstance(value, str) else ""


def _sub_mapping(mapping: dict, key: str) -> dict | None:
    """Return ``mapping[key]`` when it is itself a mapping, else ``None``."""
    value = mapping.get(key)
    return value if isinstance(value, dict) else None


def slack_open_dm(poster: Poster, bot_token: str, user_id: str) -> str:
    """Open the user's DM and return its channel id (``""`` on failure).

    ``conversations.open`` is idempotent (re-opening the same DM returns the
    same channel) so ``idempotent=True`` is the correct classification.
    """
    try:
        resp = poster("conversations.open", token=bot_token, json={"users": user_id}, idempotent=True)
    except Exception:  # noqa: BLE001 — a Slack API failure degrades to no channel id
        return ""
    return _str_field(_sub_mapping(resp, "channel"), "id")


def slack_post_message(poster: Poster, channel: str, text: str, *, bot_token: str, thread_ts: str = "") -> str:
    """Post ``text`` to ``channel``. Return the posted ``ts`` (``""`` on failure).

    The truthiness contract is preserved for callers that branch on the
    result (a non-empty ts is truthy, ``""`` is falsy), and the ts is the
    (tool_use_id, slack_ts) link the #1174 reply matcher needs.

    ``chat.postMessage`` is NOT idempotent (``idempotent=False``): a lost
    response after Slack accepted the post must not be replayed into a
    double-post — the hardened poster surfaces the failure rather than retry.
    """
    body: dict[str, str] = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    try:
        resp = poster("chat.postMessage", token=bot_token, json=body, idempotent=False)
    except Exception:  # noqa: BLE001 — a Slack API failure degrades to empty
        return ""
    if resp.get("ok") is not True:
        return ""
    return _str_field(resp, "ts")


def slack_post_dm(poster: Poster, resolve_thread: ThreadResolver, bot_token: str, user_id: str, text: str) -> str:
    """Post ``text`` to ``user_id``'s DM. Resolves channel via cache when possible.

    Cache hit → single ``chat.postMessage`` call (sub-second on a normal
    connection, fits inside the hook timeout). Cache miss or
    ``channel_not_found`` → open the DM, cache the channel id, retry.
    Threads under the user's active DM conversation when one exists (via the
    injected ``resolve_thread``). Returns the posted ``ts`` (``""`` on
    failure) for the #1174 matcher.
    """
    cached = read_dm_channel_cache(user_id)
    if cached:
        thread_ts = resolve_thread(cached)
        ts = slack_post_message(poster, cached, text, bot_token=bot_token, thread_ts=thread_ts)
        if ts:
            return ts
    channel = slack_open_dm(poster, bot_token, user_id)
    if not channel:
        return ""
    thread_ts = resolve_thread(channel)
    ts = slack_post_message(poster, channel, text, bot_token=bot_token, thread_ts=thread_ts)
    if ts:
        write_dm_channel_cache(user_id, channel)
    return ts


def _enrich_delivered_dm(
    enrich_audio: AudioEnricher | None,
    resolve_thread: ThreadResolver,
    user_id: str,
    text: str,
) -> None:
    """Attach audio to the just-delivered question DM — best-effort, never raises (#2171).

    Called by :func:`perform_slack_post` only after a confirmed post, so the
    delivered channel is the one now in the DM cache. A ``None`` enricher
    (``speak.slack`` off) is a no-op — today's text-only mirror — and any
    failure inside the injected enricher is swallowed so a synthesis / upload
    problem can NEVER retroactively break the text question that already landed.
    """
    if enrich_audio is None:
        return
    channel = read_dm_channel_cache(user_id)
    if not channel:
        return
    try:
        enrich_audio(channel, text, resolve_thread(channel))
    except Exception:  # noqa: BLE001 — audio is a pure enhancement; the text question already landed
        return


def perform_slack_post(
    slack_cfg: tuple[str, str],
    questions: list[dict],
    *,
    poster: Poster,
    resolve_thread: ThreadResolver,
    enrich_audio: AudioEnricher | None = None,
) -> str:
    """Resolve the bot token and post the question — runs synchronously.

    Synchronous so the Slack DM lands **before** the AskUserQuestion prompt
    renders in the terminal. The previous fork-and-detach variant caused the
    message to arrive *after* the user had already answered. Returns the
    posted ``ts`` (``""`` on any failure) so the #1174 capture path can link
    the mirror row to its DM.

    ``poster`` (the Slack ``SlackHttpClient.post``), ``resolve_thread`` (the
    ``IncomingEvent`` active-DM lookup) and ``enrich_audio`` (the
    ``deliver_user_dm_sidecar`` wrapper, #2171) are injected by the router so
    this transport stays a pure ``teatree.hooks`` leaf with no upward import.
    On a successful post the audio enrichment fires against the delivered
    channel so BOTH question surfaces carry audio to the user's phone.
    """
    token_ref, user_id = slack_cfg
    result = run_allowed_to_fail(["pass", "show", f"{token_ref}-bot"], expected_codes=None, timeout=2)
    bot_token = result.stdout.strip() if result.returncode == 0 else ""
    if not bot_token:
        return ""
    text = format_question_text(questions)
    ts = slack_post_dm(poster, resolve_thread, bot_token, user_id, text)
    if ts:
        _enrich_delivered_dm(enrich_audio, resolve_thread, user_id, text)
    return ts
