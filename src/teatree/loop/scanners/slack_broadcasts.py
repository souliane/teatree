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
    reviewer dispatch. This is an *outcome* reaction (review-DONE), deduped
    against existing reactors and the ``OutboundClaim`` ledger so it is
    posted at most once. The agent does not re-review already-done work.
* **At least one open MR** → emit one ``slack.review_intent`` signal per
    open MR (the dispatcher routes each to the ``t3:reviewer`` agent). No
    ``:eyes:`` reaction is posted: a claim reaction must appear only at
    review-DONE, never at discovery (#113/#86). The review-intent dispatch
    itself is gated on the review-loop-enabled state, so a stopped review
    loop queues nothing (#79).

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
The production wiring is :class:`BackendChannelHistoryFetcher` (delegating
to :meth:`MessagingBackend.fetch_channel_history`) and
:class:`GlabGhMrStateClassifier` (``glab mr view`` / ``gh pr view``); the
tick-jobs builder resolves the overlay's broadcast channel list and passes
it in, keeping the scanner overlay-agnostic.
"""

import json
import logging
import os
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast

from django.db import OperationalError, ProgrammingError

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import BroadcastObservation, ScannedBroadcast
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.core.review.author_trust import classify_author
from teatree.core.review.review_candidate import eyes_reacted_by_other
from teatree.loop.review_claim_signals import (
    filter_review_intent_signals,
    reaction_already_present,
    record_reaction_claim,
)
from teatree.loop.review_request_tracker import record_review_request_post
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass, ScanSignal, classify_gh_stderr
from teatree.types import RawAPIDict
from teatree.url_classify import Forge, find_pr_urls, forge_of, repo_and_iid
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)

# #1295 cap B: a broadcast that @-mentions the user's own Slack id is the
# auto-pickup trigger. The Slack message format is ``<@U_USER_ID>``; the
# scanner records the assignment intent so the dispatcher's mechanical
# action can assign the user as reviewer on the MR.
_SLACK_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


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

    ``author_username`` carries the MR author's forge username (GitLab
    ``author.username`` / GitHub ``user.login``) so the scanner can skip
    the ``:eyes:`` review reaction on the user's own MR broadcasts (#1384).
    Empty when the classifier could not read it — treated as "not mine".
    """

    url: str
    merged: bool
    approved: bool
    author_username: str = ""


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
    for raw in find_pr_urls(text):
        url = raw.rstrip("/").split("#")[0]
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


def _first_url(states: Sequence[MrState]) -> str:
    return states[0].url if states else ""


def _seed_review_request_posts(
    *,
    channel: str,
    ts: str,
    states: Sequence[MrState],
) -> None:
    """Seed a ``ReviewRequestPost`` for every open MR in a broadcast (#1256).

    The ``ReviewNagScanner`` walks ``ReviewRequestPost`` rows; before #1256
    only the bot's review-request flow wrote those rows, so manually-posted
    MRs in the review channel escaped the +1/+2/+3/+5d nag cadence. Seeding
    here closes that gap — the broadcast scanner is the single ingestion
    point for any colleague- or author-broadcast.

    Idempotent on ``mr_url`` via ``record_review_request_post``: a re-scan
    refreshes the channel/thread reference but preserves ``last_nag_step``
    and ``done_at`` so the nag state machine is not reset. Merged URLs are
    skipped — only open MRs need nagging.
    """
    for state in _open_subset(states):
        record_review_request_post(
            mr_url=state.url,
            slack_channel_id=channel,
            slack_thread_ts=ts,
        )


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
    # #1295 cap B: when set, broadcasts mentioning ``<@user_slack_id>``
    # emit an extra ``review_request_in_slack`` signal so the dispatcher
    # mechanically assigns the user as reviewer on the MR. Default empty
    # disables auto-pickup so legacy callers keep their previous behaviour.
    user_slack_id: str = ""
    reviewer_username: str = ""
    # #1384: the current user's forge username. When every open MR in a
    # broadcast is authored by this username the ``:eyes:`` review reaction
    # and reviewer-dispatch signals are skipped — you don't review your own
    # MR. Empty disables the filter (legacy callers keep reacting on every
    # pending broadcast). Sibling of #1321's review-sweep own-author exclusion.
    current_gitlab_username: str = ""
    name: str = field(default="slack_broadcasts", init=False)

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        try:
            for channel in self.channels:
                # F5.5: isolate each channel — one channel's fetch/handle failure
                # must not starve the channels queued after it. ScannerError
                # (auth / rate-limit from the classifier) and the DB-not-migrated
                # OperationalError/ProgrammingError still propagate: they are
                # not per-channel faults and belong to the dispatcher's #1287
                # error surface / the pre-migration skip below.
                try:
                    signals.extend(self._scan_channel(channel))
                except (ConnectChannelBotRestrictedError, ScannerError, OperationalError, ProgrammingError):
                    raise
                except Exception:
                    logger.exception("SlackBroadcastsScanner failed on channel %s", channel)
                    continue
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
            ts = message.get("ts", "<unknown>")
            try:
                signals.extend(self._handle_message(channel, message))
            except (ConnectChannelBotRestrictedError, ScannerError):
                # F5.3: a classifier auth / rate-limit failure is a recoverable
                # scanner-wide error, not a per-message parse fault — surface it
                # to the dispatcher (#1287) instead of masking it as a skipped
                # message. Connect-restriction is likewise loud (#1131).
                raise
            except Exception:
                logger.exception("SlackBroadcastsScanner failed on message %s in %s", ts, channel)
                continue
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
        # #1256: seed a ReviewRequestPost for every open MR in the broadcast
        # so the ReviewNagScanner picks them up — manual broadcasts in the
        # review channel were previously invisible to the nag train because
        # only the bot's review-request flow wrote ReviewRequestPost rows.
        _seed_review_request_posts(channel=row.channel, ts=row.slack_ts, states=states)
        signals = self._apply_classification(row, states, message, user_named=self._user_named(text))
        # #1295 cap B: detect ``<@user_slack_id>`` mentions so the
        # mechanical assigner picks up the MR without waiting for a
        # forge-side assignment to land.
        signals.extend(self._pickup_signals(text, row, states))
        return signals

    def _user_named(self, text: str) -> bool:
        user_id = self._user_id()
        if not user_id:
            return False
        return user_id in {match.group(1) for match in _SLACK_MENTION_RE.finditer(text)}

    def _pickup_signals(
        self,
        text: str,
        row: ScannedBroadcast,
        states: Sequence[MrState],
    ) -> list[ScanSignal]:
        if not self.user_slack_id or not self.reviewer_username:
            return []
        mentioned = {match.group(1) for match in _SLACK_MENTION_RE.finditer(text)}
        if self.user_slack_id not in mentioned:
            return []
        open_states = _open_subset(states)
        if self._all_authored_by_me(open_states):
            # You never assign a reviewer on your own MR — reviewers must NEVER
            # be directly assigned on the user's own GitLab MRs. Mirrors the
            # own-author skip in ``_apply_classification`` (#1384); without it a
            # broadcast that @-mentions the user on the user's OWN MR emitted a
            # ``review_request_in_slack`` pickup that mechanically assigned a
            # reviewer on it (forbidden).
            return []
        return [
            ScanSignal(
                kind="review_request_in_slack",
                summary=f"Review request via Slack mention: {state.url}",
                payload={
                    "url": state.url,
                    "mr_url": state.url,
                    "channel": row.channel,
                    "ts": row.slack_ts,
                    "reviewer_username": self.reviewer_username,
                    "overlay": self.overlay,
                    "broadcast_id": row.pk,
                },
            )
            for state in open_states
        ]

    def _apply_classification(
        self,
        row: ScannedBroadcast,
        states: Sequence[MrState],
        message: RawAPIDict,
        *,
        user_named: bool,
    ) -> list[ScanSignal]:
        if row.classification == ScannedBroadcast.Classification.ALL_MERGED:
            self._react_done(
                row.channel,
                row.slack_ts,
                "white_check_mark",
                message=message,
                target=_first_url(states) or row.slack_ts,
            )
            # #1295 cap C: cross-channel sweep — once a broadcast resolves
            # to all-merged on its own channel, replicate the
            # ``:white_check_mark:`` to every other broadcast channel that
            # also carries one of the same MR URLs, skipping channels
            # where the reaction is already present.
            self._sweep_white_check_mark(row, states)
            return []
        open_states = _open_subset(states)
        if self._all_authored_by_me(open_states):
            # #1384: every open MR in this broadcast is the user's own — there
            # is nothing to dispatch a reviewer for on one's own MR.
            return []
        if not user_named and eyes_reacted_by_other(message, user_id=self._user_id()):
            # A colleague has already :eyes:-claimed this review. Dispatching
            # ``t3:reviewer`` anyway duplicates their in-flight work. An
            # explicit ``<@user_slack_id>`` mention re-opens dispatch.
            return []
        # #113/#86: the ``:eyes:`` reaction is a CLAIM and must not be posted at
        # discovery time — only when a review is DONE (the FSM transition path
        # posts the outcome reaction). #79: a review-intent dispatch is the
        # work-queue signal; when the review loop is stopped it must not fire.
        return filter_review_intent_signals(
            _signal_for_pending_mr(state, row, overlay=self.overlay) for state in open_states
        )

    def _user_id(self) -> str:
        return getattr(self.backend, "user_id", "") or self.user_slack_id

    def _all_authored_by_me(self, open_states: Sequence[MrState]) -> bool:
        """True when every open MR is authored by a trusted identity (#1384, #1773).

        The own-author exclusion is a trusted-SET check: an MR by any of the
        user's identities (DB rows, config fallback, or the configured
        ``current_gitlab_username``) is their own work, so eyes/dispatch skip.
        """
        if not open_states:
            return False
        return all(self._author_is_trusted(state) for state in open_states)

    def _author_is_trusted(self, state: MrState) -> bool:
        author = state.author_username
        if not author:
            return False
        if self.current_gitlab_username and author == self.current_gitlab_username:
            return True
        ref = pr_ref_from_url(state.url)
        if ref is None:
            return False
        return classify_author(ref.slug, author, host_kind=ref.host_kind).trusted

    def _sweep_white_check_mark(self, row: ScannedBroadcast, states: Sequence[MrState]) -> None:
        """Re-react ``:white_check_mark:`` on sibling broadcasts of the same MRs (#1295 cap C).

        Uses each MR URL as a search anchor across every configured
        channel — if a sibling broadcast already carries the green-check
        from the user's identity we skip (idempotent), otherwise we
        react. Best-effort: any failure is logged and the rest of the
        sweep continues so one flaky channel cannot wedge the others.
        """
        mr_urls = {state.url for state in states}
        for sibling_row in ScannedBroadcast.objects.filter(
            classification=ScannedBroadcast.Classification.ALL_MERGED,
            overlay=self.overlay,
        ).exclude(pk=row.pk):
            sibling_urls = sibling_row.mr_urls if isinstance(sibling_row.mr_urls, list) else []
            if not mr_urls.intersection(sibling_urls):
                continue
            self._react_done(
                sibling_row.channel,
                sibling_row.slack_ts,
                "white_check_mark",
                message=None,
                target=_first_url(states) or sibling_row.slack_ts,
            )

    def _react_done(self, channel: str, ts: str, emoji: str, *, message: RawAPIDict | None, target: str) -> None:
        """Post an outcome reaction once, gated+routed, deduped against existing reactors + the ledger.

        Routes through :class:`OnBehalfSlackEgress` so the colleague/Connect
        broadcast channel reaction goes out under the #1750-routed personal
        ``xoxp`` token (previously a bot-token ``react`` that could not tell a
        colleague channel from the self DM) and is gated+audited like every
        other colleague-surface egress. A BLOCK verdict skips the reaction.

        #113/#123: skip when the emoji is already present (colleague or bot)
        or recorded in the :class:`OutboundClaim` ledger, so a reaction is
        never double-posted across reactors or re-fired on a later tick.
        Records the claim on success so the next tick dedups against it.
        """
        if reaction_already_present(message=message, channel=channel, ts=ts, emoji=emoji):
            return
        try:
            OnBehalfSlackEgress(self.backend).react(
                channel=channel,
                ts=ts,
                emoji=emoji,
                target=target,
                action=f"broadcast_outcome_reaction:{emoji}",
                destination=f"broadcast channel {channel}",
                summary=":white_check_mark: all-merged outcome",
            )
        except OnBehalfPostBlockedError as blocked:
            logger.info("SlackBroadcastsScanner: outcome reaction gated on %s/%s: %s", channel, ts, blocked)
            return
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
            return
        record_reaction_claim(channel=channel, ts=ts, emoji=emoji)


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


def _classifier_error(error_class: ScannerErrorClass, detail: str) -> ScannerError:
    """Build the :class:`ScannerError` the classifier raises on a non-verdict failure (F5.3)."""
    return ScannerError(scanner="slack_broadcasts", error_class=error_class, detail=detail)


def _parse_classifier_json(stdout: str, *, tool: str, url: str) -> RawAPIDict:
    """Parse a classifier subprocess's JSON object, raising :class:`ScannerError` on garbage (F5.3).

    Empty output, non-JSON text, and a well-formed-but-non-object payload are
    all failures-to-reach-a-verdict, not "not merged": the caller must not read
    ``merged=False`` out of them. Only a parsed JSON *object* is a verdict.
    """
    if not stdout.strip():
        raise _classifier_error(ScannerErrorClass.UNKNOWN, f"{tool} {url!r} returned empty output")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise _classifier_error(ScannerErrorClass.UNKNOWN, f"{tool} {url!r} returned non-JSON output: {exc}") from exc
    if not isinstance(data, dict):
        raise _classifier_error(ScannerErrorClass.UNKNOWN, f"{tool} {url!r} returned non-object JSON")
    return cast("RawAPIDict", data)


def _classifier_str(data: RawAPIDict, key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _classifier_int(data: RawAPIDict, key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) else 0


def _classifier_author(data: RawAPIDict, *, key: str) -> str:
    """The forge author's username from ``data["author"][key]`` (GitLab ``username`` / GitHub ``login``)."""
    author = data.get("author")
    if not isinstance(author, dict):
        return ""
    value = cast("RawAPIDict", author).get(key)
    return value if isinstance(value, str) else ""


@dataclass(slots=True)
class GlabGhMrStateClassifier:
    """Production :class:`MrStateClassifier` — shells out to ``glab`` / ``gh``.

    Each URL is dispatched by host: ``glab mr view <url> -F json`` for
    GitLab merge requests, ``gh pr view <url> --json …`` for GitHub
    pulls. The classifier reads ``state`` (merged-or-not) and a coarse
    approval flag (GitLab ``upvotes > 0``, GitHub
    ``reviewDecision == APPROVED``).

    Transient vs verdict (F5.3): ``merged=False`` is a *verdict* — the tool
    ran, returned a parseable payload, and the MR was not merged — so the
    scanner may safely dispatch a reviewer / seed a nag row for it. A
    FAILURE to reach that verdict (the binary is missing, the token is
    expired / rc≠0, or the output is not parseable JSON) is NOT a verdict:
    the classifier raises :class:`ScannerError` so the dispatcher records
    the degradation (#1287) and skips the tick, instead of silently
    classifying a possibly-MERGED MR as open — which would nag reviewers
    about already-landed work. A URL that is not a recognised MR (unparsable
    forge / IID) stays ``merged=False`` (it is a deterministic non-match, not
    a transient failure).

    Tokens are optional: when set they're exported as ``GITLAB_TOKEN`` /
    ``GH_TOKEN`` for each subprocess so a private-repo overlay can
    classify on behalf of its own PAT.
    """

    glab_token: str = ""
    github_token: str = ""

    def __call__(self, urls: Sequence[str]) -> list[MrState]:
        return [self._classify_one(url) for url in urls]

    def _classify_one(self, url: str) -> MrState:
        forge = forge_of(url)
        if forge is Forge.GITLAB:
            return self._classify_gitlab(url)
        if forge is Forge.GITHUB:
            return self._classify_github(url)
        return MrState(url=url, merged=False, approved=False)

    def _classify_gitlab(self, url: str) -> MrState:
        from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415 — deferred: loaded at tick time, not import

        parsed = repo_and_iid(url)
        if parsed is None:
            return MrState(url=url, merged=False, approved=False)
        project, iid_num = parsed
        iid = str(iid_num)
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
        except FileNotFoundError as exc:
            raise _classifier_error(ScannerErrorClass.UNKNOWN, f"glab not installed for {url!r}: {exc}") from exc
        if result.returncode != 0:
            raise _classifier_error(
                classify_gh_stderr(result.stderr),
                f"glab mr view {url!r} rc={result.returncode}: {result.stderr.strip()[:200]}",
            )
        data = _parse_classifier_json(result.stdout, tool="glab mr view", url=url)
        state = _classifier_str(data, "state").lower()
        merged = state in {"merged", "closed_as_merged"}
        upvotes = _classifier_int(data, "upvotes")
        approved = upvotes > 0 or merged
        author_username = _classifier_author(data, key="username")
        return MrState(url=url, merged=merged, approved=approved, author_username=author_username)

    def _classify_github(self, url: str) -> MrState:
        from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415 — deferred: loaded at tick time, not import

        gh = shutil.which("gh") or "gh"
        env = {**os.environ, "GH_TOKEN": self.github_token} if self.github_token else None
        try:
            result = run_allowed_to_fail(
                [gh, "pr", "view", url, "--json", "state,reviewDecision,author"],
                expected_codes=None,
                env=env,
            )
        except FileNotFoundError as exc:
            raise _classifier_error(ScannerErrorClass.UNKNOWN, f"gh not installed for {url!r}: {exc}") from exc
        if result.returncode != 0:
            raise _classifier_error(
                classify_gh_stderr(result.stderr),
                f"gh pr view {url!r} rc={result.returncode}: {result.stderr.strip()[:200]}",
            )
        data = _parse_classifier_json(result.stdout, tool="gh pr view", url=url)
        state = _classifier_str(data, "state").upper()
        review_decision = _classifier_str(data, "reviewDecision").upper()
        merged = state == "MERGED"
        approved = review_decision == "APPROVED" or merged
        author_username = _classifier_author(data, key="login")
        return MrState(url=url, merged=merged, approved=approved, author_username=author_username)


def _signal_for_pending_mr(state: MrState, row: ScannedBroadcast, *, overlay: str) -> ScanSignal:
    """Build the ``slack.review_intent`` signal for one open MR in a broadcast.

    Reuses the existing signal shape so the dispatcher routes through
    ``review_request_dispatch`` to the ``t3:reviewer`` agent — no new
    signal kind, no parallel dispatch path. An untrusted author on a PUBLIC
    repo flags the signal ADVERSARIAL (#1773) so the reviewer treats it as a
    potential malicious actor rather than a colleague MR.
    """
    mr_url = state.url
    ref = pr_ref_from_url(mr_url)
    untrusted = ref is not None and classify_author(ref.slug, state.author_username, host_kind=ref.host_kind).untrusted
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
            "adversarial": untrusted,
            "requires_human_authorization": untrusted,
        },
    )
