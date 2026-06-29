"""Reply transport — the outbound half of the autonomous-events loop (#654).

Every place teatree posts on behalf of the user (Slack thread reply,
Slack DM, GitLab MR comment, GitHub PR comment) goes through a
``Replier`` so the audit trail in ``ReplyDispatch`` is canonical and
idempotency keys are enforced.

`_BaseReplier` owns the shared contract: build a `ReplySpec`, short
out on a duplicate idempotency key, call the subclass `_deliver` hook,
and record the outcome (`sent` on success, `failed` + `error_message`
on any exception). Subclasses implement exactly one method —
`_deliver` — which performs the platform API call and raises on
failure. `NoopReplier` (the default for dev/tests and the fallback when
no backend is configured) delivers nothing and records `sent`. The
`replier_for` factory picks the production subclass for an
`IncomingEvent.source`, falling back to `NoopReplier` when the matching
backend was not injected (the loop scanner stays functional; wiring
real per-overlay backends is tracked separately).
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol

from django.db import IntegrityError, transaction

from teatree.core.gates.privacy_gate import PrivacyGateResult, scan_outbound_text
from teatree.core.models import IncomingEvent, ReplyDispatch
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError, require_on_behalf_approval
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post
from teatree.slack_mrkdwn import normalize_slack_message, slack_linkify

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
    from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


class PublicationPrivacyBlockedError(RuntimeError):
    """A reply body tripped the #1295 publication privacy gate — it must NOT egress.

    Raised on the on-behalf delivery path so a finding fails CLOSED: in
    :meth:`_BaseReplier._send` it is caught and converted to a FAILED
    ``ReplyDispatch``, and on the :meth:`_BaseReplier.redeliver` retry path it
    propagates to the sweep, which keeps the row FAILED until the body is
    redacted. Builds its own message from the gate result (mirroring
    ``OnBehalfPostBlockedError``).
    """

    def __init__(self, result: PrivacyGateResult) -> None:
        names = ", ".join(sorted({match.pattern_name for match in result.matches}))
        super().__init__(
            f"publication privacy gate refused: {result.target_repo} is public and the body "
            f"trips {len(result.matches)} privacy pattern(s) ({names}); redact before posting."
        )


class _ProjectInfoLike(Protocol):
    project_id: int


class GitLabNoteClient(Protocol):
    """The GitLab REST surface :class:`GitLabReplier` needs to post an MR note.

    Structural so ``core`` types the replier against a capability rather than
    importing the concrete ``backends.gitlab.api.GitLabAPI`` (#1922).
    """

    def resolve_project(self, repo_path: str) -> _ProjectInfoLike | None: ...  # pragma: no branch

    def post_json(
        self,
        endpoint: str,
        payload: "RawAPIDict | None" = None,
    ) -> "RawAPIDict | None": ...  # pragma: no branch


@dataclass(slots=True)
class ReplySpec:
    event: IncomingEvent
    target_ref: str
    body: str
    idempotency_key: str
    action_name: str


class Replier(Protocol):
    def post_in_thread(
        self,
        *,
        event: IncomingEvent,
        target_ref: str,
        thread_ref: str,
        body: str,
        idempotency_key: str,
    ) -> ReplyDispatch: ...

    def post_dm(
        self,
        *,
        event: IncomingEvent,
        actor: str,
        body: str,
        idempotency_key: str,
    ) -> ReplyDispatch: ...

    def post_comment(
        self,
        *,
        event: IncomingEvent,
        target_ref: str,
        body: str,
        idempotency_key: str,
    ) -> ReplyDispatch: ...

    def redeliver(self, dispatch: ReplyDispatch) -> None: ...


#: Code-host sources (``channel_ref`` is a repo slug) → the visibility probe's forge.
#: A Slack/CI source has no repo target, so it is absent and scoped OUT of the gate.
_FORGE_BY_SOURCE: dict[str, str] = {
    IncomingEvent.Source.GITHUB: "github",
    IncomingEvent.Source.GITLAB: "gitlab",
}


def _enforce_privacy(spec: ReplySpec) -> None:
    """Raise :class:`PublicationPrivacyBlockedError` if *spec.body* trips the gate.

    The send-time chokepoint for the #1295 gate on the colleague code-host
    surfaces: a GitHub PR comment / GitLab MR note carries the body to a repo
    that may be PUBLIC, so it is scanned for the overlay's redact-terms plus the
    built-in quote anchors before the wire call. Scoped to code-host events only
    (:data:`_FORGE_BY_SOURCE`) — a Slack thread reply carries a channel ref, not
    a repo slug, so it is out of scope here (and never mis-classified as a public
    repo). :func:`scan_outbound_text` then derives public-ness from the visibility
    axis, so a provably-private repo is a clean pass. Shared by ``_send`` (caught
    → FAILED) and ``redeliver`` (propagates to the retry sweep).
    """
    forge = _FORGE_BY_SOURCE.get(spec.event.source)
    if forge is None:
        return
    result = scan_outbound_text(text=spec.body, target_repo=spec.event.channel_ref, forge=forge)
    if result.refused:
        raise PublicationPrivacyBlockedError(result)


class _BaseReplier:
    """Shared post_* dispatch, idempotency, and outcome recording.

    Subclasses override only :meth:`_deliver`. It performs the platform
    API call and raises on failure; the base records ``sent``/``failed``.
    """

    #: Actions that post under the user's identity to a colleague/customer
    #: surface — gated by ``on_behalf_post_mode`` (#960, BLOCK under ``ask``
    #: / ``draft_or_ask``). ``post_dm`` is a bot→user message and is
    #: intentionally absent (never gated).
    _ON_BEHALF_ACTIONS: ClassVar[frozenset[str]] = frozenset({"post_in_thread", "post_comment"})

    def post_in_thread(
        self,
        *,
        event: IncomingEvent,
        target_ref: str,
        thread_ref: str,
        body: str,
        idempotency_key: str,
    ) -> ReplyDispatch:
        composite_target = f"{target_ref}/{thread_ref}" if thread_ref else target_ref
        return self._send(
            ReplySpec(
                event=event,
                target_ref=composite_target,
                body=body,
                idempotency_key=idempotency_key,
                action_name="post_in_thread",
            ),
        )

    def post_dm(
        self,
        *,
        event: IncomingEvent,
        actor: str,
        body: str,
        idempotency_key: str,
    ) -> ReplyDispatch:
        return self._send(
            ReplySpec(
                event=event,
                target_ref=actor,
                body=body,
                idempotency_key=idempotency_key,
                action_name="post_dm",
            ),
        )

    def post_comment(
        self,
        *,
        event: IncomingEvent,
        target_ref: str,
        body: str,
        idempotency_key: str,
    ) -> ReplyDispatch:
        return self._send(
            ReplySpec(
                event=event,
                target_ref=target_ref,
                body=body,
                idempotency_key=idempotency_key,
                action_name="post_comment",
            ),
        )

    def _send(self, spec: ReplySpec) -> ReplyDispatch:
        # Idempotency is intrinsic to the ReplyDispatch *record*: the
        # idempotency key is atomically reserved with a PENDING row
        # *before* the side effect, so a second caller racing on the same
        # key (including a non-loop / cron caller without the machine-wide
        # `t3 loop tick` flock #676) reuses the reserved row and never
        # re-delivers. The IntegrityError recovery below remains as
        # defense-in-depth for the create-vs-create race.
        try:
            with transaction.atomic():
                dispatch, created = ReplyDispatch.objects.get_or_create(
                    idempotency_key=spec.idempotency_key,
                    defaults={
                        "event": spec.event,
                        "target_ref": spec.target_ref,
                        "action_name": spec.action_name,
                        "status": ReplyDispatch.Status.PENDING,
                        "body": spec.body,
                    },
                )
        except IntegrityError:
            logger.debug("Reply %s already recorded — idempotent no-op", spec.idempotency_key)
            return ReplyDispatch.objects.get(idempotency_key=spec.idempotency_key)
        if not created:
            logger.debug("Reply %s already recorded — idempotent no-op", spec.idempotency_key)
            return dispatch
        if spec.action_name in self._ON_BEHALF_ACTIONS:
            try:
                _enforce_privacy(spec)
                # #1879: consume + deliver + audit run in one transaction.atomic
                # inside the gate, so a delivery failure rolls back the consume
                # (the approval is never burned) and no audit lies. A BLOCK with
                # no recorded approval raises before any wire call.
                posted_ref = require_on_behalf_approval(
                    target=spec.target_ref,
                    action=spec.action_name,
                    publish=lambda: self._deliver(spec),
                )
            except OnBehalfPostBlockedError as blocked:
                # Surface, never silently drop and never post unattended: the
                # FAILED row + actionable message is the user-notify path —
                # the retry sweep re-attempts once a user records the
                # OnBehalfApproval (no TTY) and the gate then passes.
                logger.warning("Reply %s gated by on_behalf_post_mode", spec.idempotency_key)
                return self._finalize(
                    dispatch,
                    status=ReplyDispatch.Status.FAILED,
                    error_message=str(blocked),
                )
            except Exception as exc:  # noqa: BLE001 — any backend failure becomes a FAILED row
                logger.warning("Reply %s delivery failed: %s", spec.idempotency_key, exc)
                return self._finalize(dispatch, status=ReplyDispatch.Status.FAILED, error_message=str(exc))
        else:
            try:
                # Savepoint so a DB-level error raised inside subclass
                # `_deliver` (e.g. a create-vs-create race) does not poison
                # the outer transaction and the FAILED finalize can still run.
                with transaction.atomic():
                    posted_ref = self._deliver(spec)
            except Exception as exc:  # noqa: BLE001 — any backend failure becomes a FAILED row
                logger.warning("Reply %s delivery failed: %s", spec.idempotency_key, exc)
                return self._finalize(dispatch, status=ReplyDispatch.Status.FAILED, error_message=str(exc))
        if spec.action_name in self._ON_BEHALF_ACTIONS:
            # #949: after-receipt visibility DM. Fires only for the same
            # colleague-visible actions the pre-gate scopes (post_dm /
            # internal writes excluded). Never raises, never rolls back —
            # the SENT finalize below is unreachable until it returns.
            notify_user_on_behalf_post(
                target=spec.target_ref,
                action=spec.action_name,
                destination=spec.target_ref,
                artifact_url=posted_ref or spec.target_ref,
                summary=spec.body,
            )
        return self._finalize(dispatch, status=ReplyDispatch.Status.SENT)

    @staticmethod
    def _finalize(
        dispatch: ReplyDispatch,
        *,
        status: ReplyDispatch.Status,
        error_message: str = "",
    ) -> ReplyDispatch:
        dispatch.status = status
        dispatch.error_message = error_message
        with transaction.atomic():
            dispatch.save(update_fields=["status", "error_message"])
        return dispatch

    def redeliver(self, dispatch: ReplyDispatch) -> None:
        """Re-attempt a previously-recorded dispatch (the retry-sweep path).

        Rebuilds the ``ReplySpec`` from the persisted row and calls
        ``_deliver``. Raises on failure. Does NOT touch the row — the
        sweep owns the status/retry bookkeeping so the idempotency
        short-circuit in ``_send`` (which would just return the existing
        FAILED row) is bypassed. The on-behalf gate is re-checked here so a
        retry never bypasses it: if still blocked, :class:`OnBehalfPostBlockedError`
        propagates and the sweep keeps the row FAILED until the user records
        an approval.

        #1879: the consume + deliver + audit run in one ``transaction.atomic``
        inside the gate, so a redeliver that fails rolls back the consume —
        the recorded approval is REUSED across redelivers instead of a fresh
        approval being burned on every retry.
        """
        spec = ReplySpec(
            event=dispatch.event,
            target_ref=dispatch.target_ref,
            body=dispatch.body,
            idempotency_key=dispatch.idempotency_key,
            action_name=dispatch.action_name,
        )
        if dispatch.action_name in self._ON_BEHALF_ACTIONS:
            # Re-scan on retry: a leaking (or newly-matched) body stays FAILED, never egresses.
            _enforce_privacy(spec)
            posted_ref = require_on_behalf_approval(
                target=dispatch.target_ref,
                action=dispatch.action_name,
                publish=lambda: self._deliver(spec),
            )
        else:
            posted_ref = self._deliver(spec)
        if dispatch.action_name in self._ON_BEHALF_ACTIONS:
            notify_user_on_behalf_post(
                target=dispatch.target_ref,
                action=dispatch.action_name,
                destination=dispatch.target_ref,
                artifact_url=posted_ref or dispatch.target_ref,
                summary=dispatch.body,
            )

    def _deliver(self, spec: ReplySpec) -> str:
        """Perform the platform post; return the posted artifact URL/ref.

        Returns ``""`` when the subclass cannot cheaply derive a URL —
        the caller falls back to ``spec.target_ref`` for the #949
        after-receipt DM. Raises on delivery failure (the base records
        a FAILED dispatch).
        """
        raise NotImplementedError


class NoopReplier(_BaseReplier):
    """Records the dispatch as ``sent`` without any network I/O.

    Default for dev/tests and the fallback when ``replier_for`` has no
    backend for the event's source.
    """

    def _deliver(self, spec: ReplySpec) -> str:
        logger.debug("%s swallowing %d-char body for %s", type(self).__name__, len(spec.body), spec.target_ref)
        return ""


def _linkify_for_slack(body: str) -> str:
    """Deterministically rewrite bare ``!N`` / ``#N`` refs to Slack mrkdwn links.

    The send-time chokepoint for the clickable-references rule on the Slack
    surface (#654 transport). Resolution is code-only — the active overlay's
    ``resolve_mr_token`` / ``resolve_issue_token`` (DB ``PullRequest`` store
    first, repo-context construction fallback) supply each URL, so the model
    is never asked to rewrite. An unresolvable ref is left bare for the
    bare-reference gate to handle. Best-effort: any resolver/overlay failure
    returns the body unchanged so a Slack post never crashes on linkifying.
    """
    if not body:
        return body
    try:
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

        overlay = get_overlay()
    except Exception:  # noqa: BLE001 — overlay resolution is best-effort; never crash a post
        return slack_linkify(body)
    return slack_linkify(
        body,
        mr_resolver=overlay.resolve_mr_token,
        issue_resolver=overlay.resolve_issue_token,
    )


class SlackReplier(_BaseReplier):
    """Posts via the Slack Web API (``SlackBotBackend``)."""

    def __init__(self, *, bot: "MessagingBackend") -> None:
        self._bot = bot

    def _deliver(self, spec: ReplySpec) -> str:
        # post_dm carries the recipient in spec.target_ref (the explicit
        # `actor` arg), which may differ from event.actor — e.g. escalating
        # to a lead. Thread/comment replies always go back to the
        # originating event's channel/thread; the spec's target_ref there
        # is the composite recorded for audit, not a routing override.
        normalized = normalize_slack_message(_linkify_for_slack(spec.body))
        if spec.action_name == "post_dm":
            channel = self._bot.open_dm(spec.target_ref)
            if not channel:
                msg = f"could not open DM with {spec.target_ref}"
                raise RuntimeError(msg)
            self._bot.post_message(channel=channel, text=normalized, thread_ts="")
            return ""
        response = self._bot.post_message(
            channel=spec.event.channel_ref,
            text=normalized,
            thread_ts=spec.event.thread_ref,
        )
        return self._posted_permalink(channel=spec.event.channel_ref, response=response)

    def _posted_permalink(self, *, channel: str, response: "RawAPIDict") -> str:
        """Best-effort Slack permalink for the just-posted message.

        ``get_permalink`` is known to raise; on any failure (or a missing
        ``ts``) fall back to a channel deep-link. Never raises — the
        after-receipt DM tolerates an empty ref and falls back to the
        audit target.
        """
        ts = str(response.get("ts", ""))
        if not ts:
            return f"slack://channel?id={channel}"
        try:
            return self._bot.get_permalink(channel=channel, ts=ts)
        except Exception:  # noqa: BLE001 — permalink lookup is best-effort
            return f"slack://channel?id={channel}"


class GitLabReplier(_BaseReplier):
    """Posts an MR note via the GitLab REST API."""

    def __init__(self, *, client: "GitLabNoteClient") -> None:
        self._client = client

    def _deliver(self, spec: ReplySpec) -> str:
        project = self._client.resolve_project(spec.event.channel_ref)
        if project is None:
            msg = f"could not resolve GitLab project {spec.event.channel_ref}"
            raise RuntimeError(msg)
        raw_iid = spec.event.thread_ref.strip()
        if not raw_iid.isdigit():
            msg = f"GitLab MR iid is not numeric: {spec.event.thread_ref!r}"
            raise RuntimeError(msg)
        result = self._client.post_json(
            f"projects/{project.project_id}/merge_requests/{int(raw_iid)}/notes",
            {"body": spec.body},
        )
        if isinstance(result, dict):
            web_url = result.get("web_url") or result.get("html_url")
            if web_url:
                return str(web_url)
        # No note URL in the response → the canonical MR ref still names
        # where the note landed.
        return f"{spec.event.channel_ref}!{int(raw_iid)}"


class GitHubReplier(_BaseReplier):
    """Posts a PR comment via the GitHub API (``GitHubCodeHost``)."""

    def __init__(self, *, host: "CodeHostBackend") -> None:
        self._host = host

    def _deliver(self, spec: ReplySpec) -> str:
        raw_iid = spec.event.thread_ref.strip()
        if not raw_iid.isdigit():
            msg = f"GitHub PR number is not numeric: {spec.event.thread_ref!r}"
            raise RuntimeError(msg)
        result = self._host.post_pr_comment(
            repo=spec.event.channel_ref,
            pr_iid=int(raw_iid),
            body=spec.body,
        )
        web_url = result.get("html_url") or result.get("web_url")
        if web_url:
            return str(web_url)
        return f"{spec.event.channel_ref}#{int(raw_iid)}"


def replier_for(
    source: str,
    *,
    bot: "MessagingBackend | None" = None,
    gitlab: "GitLabNoteClient | None" = None,
    github: "CodeHostBackend | None" = None,
) -> Replier:
    """Pick the production replier for *source*, or ``NoopReplier``.

    The matching backend must be injected; when it is absent the loop
    scanner still functions (records the dispatch) but performs no
    network I/O. Wiring real per-overlay backends is tracked in a
    follow-up — the source alone does not identify the overlay.
    """
    if source == IncomingEvent.Source.SLACK and bot is not None:
        return SlackReplier(bot=bot)
    if source == IncomingEvent.Source.GITLAB and gitlab is not None:
        return GitLabReplier(client=gitlab)
    if source == IncomingEvent.Source.GITHUB and github is not None:
        return GitHubReplier(host=github)
    return NoopReplier()
