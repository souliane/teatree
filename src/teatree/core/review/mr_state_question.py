"""Ask the OWNER what state a merge request is in, when nothing can decide it.

This is the bot asking its own operator about the operator's own work. It is
NOT a post made as the user to a colleague, so it must never be routed through
or gated by ``on_behalf_post_mode`` / ``notify_on_post_on_behalf`` — those
govern the user's colleague-facing voice. Arming the publish gate (the shipped
default) would otherwise swallow every one of these questions at exactly the
moment the operator is most careful about what leaves their machine.

:class:`~teatree.core.models.deferred_question.DeferredQuestion` is already that
separate surface: it reaches the owner through
:func:`teatree.core.notify_question_drains.drain_unmirrored_deferred_questions`
over :func:`teatree.core.notify.notify_user`, a bot→owner path with no
``OnBehalfSlackEgress`` anywhere in it. So this module builds a row; it does not
build a channel.

Two bounds keep the surface from becoming noise. The dedupe marker is
``mr-state:<canonical url>`` built from the SAME canonicaliser the review-request
guard and the sanctioned-post command use, so one merge request can hold at most
one open question no matter how many ticks re-derive the ambiguity, and the
marker is provably the same string as every other review-request scope for that
merge request. Above ``mr_state_questions_max_per_tick`` open questions the ask
is REFUSED rather than queued, so a backlog of undecidable merge requests cannot
arrive as a flood the owner answers none of; the refused merge request is
re-offered on a later tick once a slot frees.
"""

import json
import logging
from collections.abc import Sequence

from teatree.config import get_effective_settings
from teatree.core.gates.review_request_guard import canonical_mr_url
from teatree.core.models.deferred_question import DeferredQuestion

logger = logging.getLogger(__name__)

_MARKER_PREFIX = "mr-state:"


def mr_state_marker(mr_url: str) -> str:
    """The dedupe scope for *mr_url* — one open question per canonical merge request."""
    return f"{_MARKER_PREFIX}{canonical_mr_url(mr_url)}"


def ask_mr_state(*, mr_url: str, reason: str, options: Sequence[str] = ()) -> DeferredQuestion | None:
    """Queue an owner question about *mr_url*'s state; ``None`` when the cap refuses it.

    Returns the already-open row when one exists for this merge request, so a
    re-ask is idempotent and can never be refused by the cap — a merge request
    the owner is already being asked about occupies its slot rather than
    competing for a new one.
    """
    marker = mr_state_marker(mr_url)
    open_questions = DeferredQuestion.objects.filter(
        dedupe_marker__startswith=_MARKER_PREFIX,
        answered_at__isnull=True,
        dismissed_at__isnull=True,
    )
    already_asked = open_questions.filter(dedupe_marker=marker).first()
    if already_asked is not None:
        return already_asked

    cap = get_effective_settings().mr_state_questions_max_per_tick
    if open_questions.count() >= cap:
        logger.info("mr-state question for %s deferred — %s open already (cap %s)", mr_url, open_questions.count(), cap)
        return None

    return DeferredQuestion.record(
        _question_text(mr_url=mr_url, reason=reason),
        options_json=_options_json(options),
        dedupe_marker=marker,
        audience=DeferredQuestion.Audience.OWNER_QUESTION,
    )


def _question_text(*, mr_url: str, reason: str) -> str:
    return f"I cannot determine the state of {mr_url} — {reason} How should I treat it?"


def _options_json(options: Sequence[str]) -> str:
    return json.dumps([{"label": option} for option in options]) if options else ""
