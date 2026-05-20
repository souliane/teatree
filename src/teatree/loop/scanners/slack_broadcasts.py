"""Slack review-broadcast scanner (#1131).

Polls one or more Slack channels for messages that broadcast MR/PR URLs and
queues reviewer dispatch when the MRs are still open. Sibling to
:class:`teatree.loop.scanners.slack_review_intent.SlackReviewIntentScanner`,
which handles the reaction/mention-triggered path; this scanner is the
channel-poll path for any review-team-style broadcast channel the overlay
opts into.

Behaviour per scanned broadcast
-------------------------------

The scanner extracts every MR URL from the message text, classifies the set
via the injected :class:`MrStateClassifier`, persists a
:class:`teatree.core.models.ScannedBroadcast` row (idempotent on
``(channel, slack_ts)``), and reacts on the Slack message:

* **All merged + approved** → react ``:white_check_mark:`` and skip
    reviewer dispatch. Matches the rule from #1131: the reaction is
    sufficient acknowledgement, the agent does not re-review already-done
    work.
* **At least one open MR** → react ``:eyes:`` and emit one
    ``slack.review_intent`` signal per open MR. The existing dispatcher
    routes each signal to the ``t3:reviewer`` agent — no separate
    Task-model plumbing in this PR (see follow-up #1234 / #1235 — TODO,
    filed as part of #1131's smallest-atomic-slice scope decision).

Idempotency
-----------

The :class:`ScannedBroadcast` ledger key ``(channel, slack_ts)`` makes
re-scanning safe. A re-classification (pending → all_merged once the
last open MR closes) updates the row and re-reacts; an unchanged
classification is a no-op.

Channel-list and MR-state lookup are dependency-injected
-------------------------------------------------------

Both the per-channel message fetcher and the MR-state classifier are
function parameters on the scanner, not protocol methods on the
backend. This keeps the scanner unit-testable without expanding the
:class:`MessagingBackend` protocol or shelling out to ``glab`` in tests.
The production wiring (channel-history fetcher on the Slack backend,
``glab mr view`` based classifier) lands in follow-up #1131-wiring once
the overlay extension point ``get_review_broadcast_channels()`` is
designed and merged.
"""

import json
import logging
import os
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from django.db import OperationalError, ProgrammingError

from teatree.backends.protocols import MessagingBackend
from teatree.core.models import BroadcastObservation, ScannedBroadcast
from teatree.loop.scanners.base import ScanSignal
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(r"https://[^\s|>]+/(?:merge_requests|pull|pulls)/\d+")
_GITLAB_MR_URL_RE = re.compile(
    r"^https://[^/]+/(?P<project>[^?#]+?)/-/merge_requests/(?P<iid>\d+)/?$",
)


def _parse_gitlab_mr_url(url: str) -> tuple[str, str] | None:
    """Split a GitLab MR URL into ``(project_path, iid)`` for ``glab -R <project> <iid>``.

    GitLab MR URLs are ``https://<host>/<group>/<project>/-/merge_requests/<iid>``
    (the project path can include nested subgroups: ``team/sub/api``). Outside
    a repo cwd, ``glab mr view <full-url>`` silently early-exits because
    glab refuses to resolve the host from a URL alone — the scanner
    process has no git remote to anchor against. Splitting on ``/-/``
    isolates the project path glab needs for its ``-R`` flag, and the
    numeric IID is the positional argument that pairs with it. Returns
    ``None`` when the URL doesn't match the GitLab shape (e.g. a GitHub
    URL or a malformed link), so the classifier safely falls through to
    the "couldn't confirm" default.
    """
    match = _GITLAB_MR_URL_RE.match(url)
    if match is None:
        return None
    return match.group("project"), match.group("iid")


class ConnectChannelBotRestrictedError(RuntimeError):
    """Raised when a broadcast in a Slack-Connect channel cannot be reacted to.

    The bot token is rejected on Connect channels and the dual-token
    fallback (post via the user ``xoxp``) is tracked in #1209 — until
    that lands, the scanner must hard-fail loudly rather than silently
    swallow the failed reaction. The error carries the channel id so
    callers can surface a single actionable message.
    """

    def __init__(self, channel: str) -> None:
        super().__init__(
            f"Slack-Connect channel {channel!r} rejected the bot reaction "
            "and the user-token fallback is not wired (tracked in #1209). "
            "Scanner is failing loudly per #1131 to avoid silent drops.",
        )
        self.channel = channel


@dataclass(frozen=True, slots=True)
class MrState:
    """Per-MR snapshot the classifier returns for one URL.

    ``merged`` and ``approved`` are the two flags the classifier needs to
    surface; everything else (CI status, draft flag, reviewer list) is
    out of scope for #1131 — the existing reviewer-agent pipeline
    handles those once the URL reaches it.
    """

    url: str
    merged: bool
    approved: bool


MrStateClassifier = Callable[[Sequence[str]], list[MrState]]
"""Function that maps a list of MR URLs to per-URL :class:`MrState` records.

Injected into the scanner so tests can supply a fake while the
production wiring (``glab mr view``) lands separately. The function
must preserve URL order and length — the scanner zips back to the
input list for downstream signal emission.
"""


class ChannelHistoryFetcher(Protocol):
    """Function-style fetcher for the recent messages in one channel.

    The production implementation will call ``conversations.history`` on
    the Slack backend; the test path supplies an in-memory dict. Kept
    as a Protocol so the scanner does not depend on a concrete fetcher
    class.
    """

    def __call__(self, *, channel: str) -> list[RawAPIDict]: ...


def _extract_mr_urls(text: str) -> list[str]:
    """Return every MR/PR URL in *text* in source order, deduplicated."""
    seen: dict[str, None] = {}
    for match in _PR_URL_RE.finditer(text):
        url = match.group(0).rstrip("/").split("#")[0]
        seen.setdefault(url, None)
    return list(seen)


def _classify(states: Sequence[MrState]) -> ScannedBroadcast.Classification:
    """Reduce per-MR states to one broadcast-level classification.

    ``all_merged`` requires every MR merged AND approved; any other
    combination is ``pending`` (the reviewer-dispatch path handles the
    open subset).
    """
    if not states:
        return ScannedBroadcast.Classification.PENDING
    if all(state.merged and state.approved for state in states):
        return ScannedBroadcast.Classification.ALL_MERGED
    return ScannedBroadcast.Classification.PENDING


def _open_subset(states: Sequence[MrState]) -> list[MrState]:
    return [state for state in states if not state.merged]


@dataclass(slots=True)
class SlackBroadcastsScanner:
    """Scan one or more Slack channels for MR-broadcast messages.

    *channels* is the explicit list of channel ids to poll; the
    overlay-driven channel list (per the #1131 architectural
    clarification) is resolved by the tick-jobs builder and passed in
    here, keeping the scanner overlay-agnostic. *fetch_channel_history*
    is the per-channel message fetcher; *classify_mrs* maps MR URLs to
    :class:`MrState` records.
    """

    backend: MessagingBackend
    channels: Sequence[str]
    fetch_channel_history: ChannelHistoryFetcher
    classify_mrs: MrStateClassifier
    overlay: str = ""
    name: str = field(default="slack_broadcasts", init=False)

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        try:
            for channel in self.channels:
                signals.extend(self._scan_channel(channel))
        except (OperationalError, ProgrammingError):
            # ``ScannedBroadcast`` lives in core migration 0028; an
            # install that hasn't run migrations yet raises "no such
            # table" (sqlite ``OperationalError``) or "relation does
            # not exist" (Postgres ``ProgrammingError``) the first
            # time we hit ``ScannedBroadcast.record``. Skipping
            # silently here keeps the rest of the scanner registry
            # running on a pre-migration install instead of spamming
            # a per-tick traceback. Sibling pattern lives in
            # :class:`IncomingEventsScanner`. Transient OperationalError
            # (lock timeout, connection drop) and any other DatabaseError
            # keep propagating to ``tick._run_job``.
            logger.info(
                "SlackBroadcastsScanner: teatree_scanned_broadcast unavailable (DB not migrated yet) — skipping",
            )
            return []
        return signals

    def _scan_channel(self, channel: str) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        for message in self.fetch_channel_history(channel=channel):
            signals.extend(self._handle_message(channel, message))
        return signals

    def _handle_message(self, channel: str, message: RawAPIDict) -> list[ScanSignal]:
        text = message.get("text")
        ts = message.get("ts")
        if not isinstance(text, str) or not isinstance(ts, str) or not text or not ts:
            return []
        mr_urls = _extract_mr_urls(text)
        if not mr_urls:
            return []
        states = self.classify_mrs(mr_urls)
        classification = _classify(states)
        observation = BroadcastObservation(
            channel=channel,
            slack_ts=ts,
            mr_urls=mr_urls,
            classification=classification.value,
            overlay=self.overlay,
        )
        row = ScannedBroadcast.record(observation)
        if row is None:
            return []
        return self._apply_classification(row, states)

    def _apply_classification(
        self,
        row: ScannedBroadcast,
        states: Sequence[MrState],
    ) -> list[ScanSignal]:
        if row.classification == ScannedBroadcast.Classification.ALL_MERGED:
            self._react(row.channel, row.slack_ts, "white_check_mark")
            return []
        self._react(row.channel, row.slack_ts, "eyes")
        return [_signal_for_pending_mr(state.url, row, overlay=self.overlay) for state in _open_subset(states)]

    def _react(self, channel: str, ts: str, emoji: str) -> None:
        try:
            self.backend.react(channel=channel, ts=ts, emoji=emoji)
        except ConnectChannelBotRestrictedError:
            raise
        except Exception as exc:
            # A Slack-Connect channel rejecting the bot token is the
            # specific failure #1131 must surface loudly until #1209
            # lands; the backend reports it as a generic exception, so
            # we lift it here. Any other reaction failure is logged
            # and left to the next tick.
            if _looks_like_connect_restriction(exc):
                raise ConnectChannelBotRestrictedError(channel) from exc
            logger.exception("Failed to react :%s: on %s/%s", emoji, channel, ts)


def _looks_like_connect_restriction(exc: BaseException) -> bool:
    """Heuristic for the Slack-Connect bot-restricted error shape.

    Slack's API returns ``not_in_channel`` / ``channel_not_found`` /
    ``is_archived`` for the Connect-bot-restricted case. The backend
    wraps the response in a generic exception with the error code in
    the message, so the scanner matches on the string. Once #1209
    introduces typed errors on the backend the heuristic moves to an
    ``isinstance`` check.
    """
    message = str(exc)
    return any(token in message for token in ("not_in_channel", "channel_not_found", "is_ext_shared"))


@dataclass(slots=True)
class BackendChannelHistoryFetcher:
    """Production :class:`ChannelHistoryFetcher` — delegates to the messaging backend.

    Wraps :meth:`MessagingBackend.fetch_channel_history` so the scanner
    stays overlay-agnostic and tests can keep injecting a plain
    dict-based fetcher. Returned messages are passed through unchanged
    (the backend already stamps ``channel`` on each entry).
    """

    backend: MessagingBackend
    limit: int = 50

    def __call__(self, *, channel: str) -> list[RawAPIDict]:
        return self.backend.fetch_channel_history(channel=channel, limit=self.limit)


@dataclass(slots=True)
class GlabGhMrStateClassifier:
    """Production :class:`MrStateClassifier` — shells out to ``glab`` / ``gh``.

    Each URL is dispatched by host: ``glab mr view <url> -F json`` for
    GitLab merge requests, ``gh pr view <url> --json …`` for GitHub
    pulls. The classifier reads ``state`` (merged-or-not) and a coarse
    approval flag (GitLab ``upvotes > 0``, GitHub
    ``reviewDecision == APPROVED``). Any URL that fails to parse or
    whose subprocess returns non-zero is treated as
    ``merged=False, approved=False`` so the scanner falls through to
    "open MR — please review" — the safe default for a broadcast we
    couldn't confirm.

    Tokens are optional: when set they're exported as ``GITLAB_TOKEN`` /
    ``GH_TOKEN`` for each subprocess so a private-repo overlay can
    classify on behalf of its own PAT.
    """

    glab_token: str = ""
    github_token: str = ""

    def __call__(self, urls: Sequence[str]) -> list[MrState]:
        return [self._classify_one(url) for url in urls]

    def _classify_one(self, url: str) -> MrState:
        if "/merge_requests/" in url:
            return self._classify_gitlab(url)
        if "/pull/" in url or "/pulls/" in url:
            return self._classify_github(url)
        return MrState(url=url, merged=False, approved=False)

    def _classify_gitlab(self, url: str) -> MrState:
        from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415

        parsed = _parse_gitlab_mr_url(url)
        if parsed is None:
            return MrState(url=url, merged=False, approved=False)
        project, iid = parsed
        glab = shutil.which("glab") or "glab"
        env = {**os.environ, "GITLAB_TOKEN": self.glab_token} if self.glab_token else None
        try:
            # ``-R <project>`` makes glab resolve the MR against an explicit
            # project path instead of the current cwd's git remote — the
            # scanner runs from the loop process which has no repo cwd, so
            # ``glab mr view <url>`` (URL-only) silently exits non-zero and
            # every broadcast is dropped. With ``-R`` + numeric IID glab
            # routes the API call directly.
            result = run_allowed_to_fail(
                [glab, "mr", "view", "-R", project, iid, "-F", "json"],
                expected_codes=None,
                env=env,
            )
        except FileNotFoundError:
            return MrState(url=url, merged=False, approved=False)
        if result.returncode != 0 or not result.stdout.strip():
            return MrState(url=url, merged=False, approved=False)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return MrState(url=url, merged=False, approved=False)
        if not isinstance(data, dict):
            return MrState(url=url, merged=False, approved=False)
        state = str(data.get("state", "")).lower()
        merged = state in {"merged", "closed_as_merged"}
        upvotes = int(data.get("upvotes", 0) or 0)
        approved = upvotes > 0 or merged
        return MrState(url=url, merged=merged, approved=approved)

    def _classify_github(self, url: str) -> MrState:
        from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415

        gh = shutil.which("gh") or "gh"
        env = {**os.environ, "GH_TOKEN": self.github_token} if self.github_token else None
        try:
            result = run_allowed_to_fail(
                [gh, "pr", "view", url, "--json", "state,reviewDecision"],
                expected_codes=None,
                env=env,
            )
        except FileNotFoundError:
            return MrState(url=url, merged=False, approved=False)
        if result.returncode != 0 or not result.stdout.strip():
            return MrState(url=url, merged=False, approved=False)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return MrState(url=url, merged=False, approved=False)
        if not isinstance(data, dict):
            return MrState(url=url, merged=False, approved=False)
        state = str(data.get("state", "")).upper()
        review_decision = str(data.get("reviewDecision", "")).upper()
        merged = state == "MERGED"
        approved = review_decision == "APPROVED" or merged
        return MrState(url=url, merged=merged, approved=approved)


def _signal_for_pending_mr(mr_url: str, row: ScannedBroadcast, *, overlay: str) -> ScanSignal:
    """Build the ``slack.review_intent`` signal for one open MR in a broadcast.

    Reuses the existing signal shape so the dispatcher routes through
    ``_review_request_dispatch`` to the ``t3:reviewer`` agent — no new
    signal kind, no parallel dispatch path.
    """
    return ScanSignal(
        kind="slack.review_intent",
        summary=f"Review intent (broadcast): {mr_url}",
        payload={
            "url": mr_url,
            "mr_url": mr_url,
            "channel": row.channel,
            "ts": row.slack_ts,
            "trigger": "broadcast",
            "overlay": overlay,
            "broadcast_id": row.pk,
        },
    )
