"""Phase-1 member source: the owner's Slack DMs, as USER-turn pseudo-transcript lines (#2663).

:func:`teatree.loops.dream.replay.enumerate_members` globs ``~/.claude/projects`` —
Claude Code session transcripts, curated memories, task outputs. The owner ALSO instructs
the factory through a completely separate channel, a Slack DM, and that channel had no
member source at all: every correction ever typed there was invisible to the
Instruction-Compliance Accountant (:mod:`teatree.loops.dream.compliance`), whose entire
job is to learn from exactly those corrections. The detectors were never wrong — they
were pointed at a corpus that did not contain the evidence.

This module is the missing source and nothing downstream changes. A Slack member is an
ordinary :class:`~teatree.loops.dream.replay.TranscriptMember`, so
:func:`~teatree.loops.dream.transcript_extract.looks_like_user_correction`, the weight
ladder, batching, clustering, and the promote / escalate chain all reach it for free —
including the banned-term and bare-reference withholding every published body already
passes through. No new redaction path is introduced here, deliberately: a Slack-derived
cluster is scrubbed by the SAME gate as a transcript-derived one, at the same chokepoint.

**Owner-authored only, by a POSITIVE allowlist.** Factory-authored DMs are the agent's own
output, not instructions; mining them would have the accountant grading itself and would
let its own prose be attributed as an owner rule. The allowlist is the ``slack_user_id``
recorded per overlay in the DB ``overlays`` registry — the same field every other Slack
surface uses to answer "who owns this DM" (``t3 slack dm-doctor``: *the bot cannot DM or
read its owner*). It fails CLOSED: an unresolvable owner id enumerates NOTHING rather
than falling back to "everything that is not obviously the bot", because a denylist gets
the dangerous case wrong (a blank ``user_id``, a second app, a renamed bot all pass one).
``TrustedIdentity`` is not consulted — it carries no ``slack`` rows, so keying on it would
have made this source a silent no-op that looks exactly like a working fix.

**Window.** The rows are recency-gated by the caller's own cutoff, the same one the
session/sub-agent/task globs use — a Slack DM is fresh drift, not durable doctrine, so it
belongs on the gated side of that split rather than with the always-re-read memory files.
Re-mining a month-old correction on every pass would also turn each nightly compliance
snapshot from a MEASUREMENT of its window into a running total. :data:`_MAX_ROWS` is the
belt-and-braces bound on top: the query can never become unbounded work for a pass that is
already fighting for its runtime, whatever the channel's volume grows to.

**Threading.** A Slack reply carries the thread ROOT's ts, never its parent's, so a thread
tree cannot be rebuilt from one row — and grouping BY thread would fragment the day's
conversation, which is the adjacency the repeated-user-turn keeper and any "the correction
follows the thing it corrects" reading depend on. Members are therefore grouped by UTC DAY
in ``received_at`` order, and ``thread_ts`` is stamped into the rendered envelope so the
provenance survives for a later consumer. It costs nothing in the prompt:
:func:`~teatree.loops.dream.transcript_extract.decode_transcript_line` renders only the
role and the text, so the envelope's extra keys never reach the distiller.
"""

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from teatree.core.models import ConfigSetting, PendingChatInjection
from teatree.loops.dream.replay import TranscriptMember

logger = logging.getLogger(__name__)

#: The DB ``ConfigSetting`` row holding ``{overlay: {fields}}``. Read here directly rather
#: than through ``teatree.cli.slack.app_resolve.read_overlay_registry``, which is the same
#: two lines behind an INTERFACE-layer import this ORCHESTRATION-layer module may not take.
_OVERLAYS_REGISTRY_KEY = "overlays"

#: The member ``kind`` a rendered Slack day carries. Anything that is not ``"memory"``
#: is a TRANSCRIPT everywhere downstream (the weight ladder's correction floor, the
#: ``TRANSCRIPT_FLOOR`` slice, ``compliance._correction_lines``), which is exactly the
#: treatment an owner DM should get — it is fresh drift, never durable doctrine.
SLACK_MEMBER_KIND = "slack_dm"

#: The synthetic path root each rendered day-member is identified by. Nothing is written
#: to disk — the member carries its body inline — but the path is the member's IDENTITY:
#: the distiller prompt prints it as the snippet header and it feeds the deterministic
#: cluster key, so it must be stable and must name no secret.
_MEMBER_ROOT = Path("slack-dm")

#: The hard row bound on one pass's Slack query. Far above the channel's observed volume
#: (80 rows across the first 34 days of history), so it is inert in practice — its job is
#: to guarantee the source can never become unbounded work, not to shape the corpus.
_MAX_ROWS = 500


def _owner_slack_user_ids() -> frozenset[str]:
    """Every Slack user id the OPERATOR posts under, from the DB ``overlays`` registry.

    The positive allowlist. Empty when no overlay records a ``slack_user_id`` — the
    fail-closed state, in which :func:`owner_slack_members` enumerates nothing.
    """
    registry = ConfigSetting.objects.get_effective(_OVERLAYS_REGISTRY_KEY)
    if not isinstance(registry, dict):
        return frozenset()
    ids = {str(block.get("slack_user_id", "")).strip() for block in registry.values() if isinstance(block, dict)}
    return frozenset(user_id for user_id in ids if user_id)


def render_owner_turn(text: str, *, received_at: datetime, thread_ts: str) -> str:
    r"""Render one owner Slack message as a single USER-turn transcript JSONL line.

    The shape is the Claude Code transcript envelope the rest of phase 1 already parses
    (``{"type": "user", "message": {"role": "user", "content": …}}``), because matching it
    is what makes every existing keeper work unchanged: the raw line satisfies the
    ``"(type|role)": "user"`` role gate the correction/ask detectors run on, and
    :func:`~teatree.loops.dream.transcript_extract.decode_transcript_line` flattens it to
    the same ``{"role": "user"} <text>`` form every other transcript line is reduced to.

    ``ensure_ascii=False`` so the raw line holds the owner's real characters rather than
    ``\uXXXX`` escapes — the decoder would restore them anyway, but an un-escaped body is
    also legible to a scanner reading the raw form, and nothing downstream requires ASCII.
    """
    return json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": text},
            "timestamp": received_at.isoformat(),
            "thread_ts": thread_ts,
        },
        ensure_ascii=False,
    )


def owner_slack_members(*, since: datetime) -> list[TranscriptMember]:
    """The owner's Slack DMs at/after *since*, as one member per UTC day (newest first).

    Returns ``[]`` — never raises — when the owner's Slack identity is unresolvable or the
    window holds no owner-authored message. Each member carries its rendered body inline,
    so this reads the DB but writes nothing: the corpus gains the owner's instructions
    without gaining a second plaintext copy of the DM channel on disk.
    """
    owner_ids = _owner_slack_user_ids()
    if not owner_ids:
        logger.warning(
            "dream replay: no overlay records a slack_user_id, so the owner's Slack DMs are "
            "NOT in the replay corpus this pass (failing closed rather than mining unattributed "
            "messages). Set it with `t3 setup slack-bot`.",
        )
        return []

    rows = list(
        PendingChatInjection.objects.filter(received_at__gte=since, user_id__in=owner_ids)
        .order_by("received_at")
        .values_list("text", "received_at", "thread_ts")[:_MAX_ROWS]
    )
    if len(rows) == _MAX_ROWS:
        logger.warning(
            "dream replay: the Slack member source hit its %d-row cap for this window; "
            "the oldest messages in the window are not in this pass's corpus.",
            _MAX_ROWS,
        )

    by_day: defaultdict[str, list[str]] = defaultdict(list)
    latest: dict[str, float] = {}
    for text, received_at, thread_ts in rows:
        if not text.strip():
            continue
        moment = received_at.astimezone(UTC)
        day = moment.date().isoformat()
        by_day[day].append(render_owner_turn(text, received_at=moment, thread_ts=thread_ts))
        latest[day] = max(latest.get(day, 0.0), moment.timestamp())

    members = [
        TranscriptMember(
            path=_MEMBER_ROOT / f"{day}.jsonl",
            kind=SLACK_MEMBER_KIND,
            mtime=latest[day],
            text="\n".join(lines),
        )
        for day, lines in by_day.items()
    ]
    members.sort(key=lambda member: member.mtime, reverse=True)
    return members


__all__ = ["SLACK_MEMBER_KIND", "owner_slack_members", "render_owner_turn"]
