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

from teatree.core.models import IncomingEvent, ReplyDispatch
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError, require_on_behalf_approval

if TYPE_CHECKING:
    from teatree.backends.github import GitHubCodeHost
    from teatree.backends.gitlab_api import GitLabAPI
    from teatree.backends.slack_bot import SlackBotBackend

logger = logging.getLogger(__name__)


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


class _BaseReplier:
    """Shared post_* dispatch, idempotency, and outcome recording.

    Subclasses override only :meth:`_deliver`. It performs the platform
    API call and raises on failure; the base records ``sent``/``failed``.
    """

    #: Actions that post under the user's identity to a colleague/customer
    #: surface — gated by ``ask_before_post_on_behalf`` (#960). ``post_dm``
    #: is a bot→user message and is intentionally absent (never gated).
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
                require_on_behalf_approval(target=spec.target_ref, action=spec.action_name)
            except OnBehalfPostBlockedError as blocked:
                # Surface, never silently drop and never post unattended: the
                # FAILED row + actionable message is the user-notify path —
                # the retry sweep re-attempts once a user records the
                # OnBehalfApproval (no TTY) and the gate then passes.
                logger.warning("Reply %s gated by ask_before_post_on_behalf", spec.idempotency_key)
                return self._finalize(
                    dispatch,
                    status=ReplyDispatch.Status.FAILED,
                    error_message=str(blocked),
                )
        try:
            # Savepoint so a DB-level error raised inside subclass
            # `_deliver` (e.g. a create-vs-create race) does not poison
            # the outer transaction and the FAILED finalize can still run.
            with transaction.atomic():
                self._deliver(spec)
        except Exception as exc:  # noqa: BLE001 — any backend failure becomes a FAILED row
            logger.warning("Reply %s delivery failed: %s", spec.idempotency_key, exc)
            return self._finalize(dispatch, status=ReplyDispatch.Status.FAILED, error_message=str(exc))
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
        """
        if dispatch.action_name in self._ON_BEHALF_ACTIONS:
            require_on_behalf_approval(target=dispatch.target_ref, action=dispatch.action_name)
        self._deliver(
            ReplySpec(
                event=dispatch.event,
                target_ref=dispatch.target_ref,
                body=dispatch.body,
                idempotency_key=dispatch.idempotency_key,
                action_name=dispatch.action_name,
            ),
        )

    def _deliver(self, spec: ReplySpec) -> None:
        raise NotImplementedError


class NoopReplier(_BaseReplier):
    """Records the dispatch as ``sent`` without any network I/O.

    Default for dev/tests and the fallback when ``replier_for`` has no
    backend for the event's source.
    """

    def _deliver(self, spec: ReplySpec) -> None:
        logger.debug("%s swallowing %d-char body for %s", type(self).__name__, len(spec.body), spec.target_ref)


class SlackReplier(_BaseReplier):
    """Posts via the Slack Web API (``SlackBotBackend``)."""

    def __init__(self, *, bot: "SlackBotBackend") -> None:
        self._bot = bot

    def _deliver(self, spec: ReplySpec) -> None:
        # post_dm carries the recipient in spec.target_ref (the explicit
        # `actor` arg), which may differ from event.actor — e.g. escalating
        # to a lead. Thread/comment replies always go back to the
        # originating event's channel/thread; the spec's target_ref there
        # is the composite recorded for audit, not a routing override.
        if spec.action_name == "post_dm":
            channel = self._bot.open_dm(spec.target_ref)
            if not channel:
                msg = f"could not open DM with {spec.target_ref}"
                raise RuntimeError(msg)
            self._bot.post_message(channel=channel, text=spec.body, thread_ts="")
            return
        self._bot.post_message(
            channel=spec.event.channel_ref,
            text=spec.body,
            thread_ts=spec.event.thread_ref,
        )


class GitLabReplier(_BaseReplier):
    """Posts an MR note via the GitLab REST API."""

    def __init__(self, *, client: "GitLabAPI") -> None:
        self._client = client

    def _deliver(self, spec: ReplySpec) -> None:
        project = self._client.resolve_project(spec.event.channel_ref)
        if project is None:
            msg = f"could not resolve GitLab project {spec.event.channel_ref}"
            raise RuntimeError(msg)
        raw_iid = spec.event.thread_ref.strip()
        if not raw_iid.isdigit():
            msg = f"GitLab MR iid is not numeric: {spec.event.thread_ref!r}"
            raise RuntimeError(msg)
        self._client.post_json(
            f"projects/{project.project_id}/merge_requests/{int(raw_iid)}/notes",
            {"body": spec.body},
        )


class GitHubReplier(_BaseReplier):
    """Posts a PR comment via the GitHub API (``GitHubCodeHost``)."""

    def __init__(self, *, host: "GitHubCodeHost") -> None:
        self._host = host

    def _deliver(self, spec: ReplySpec) -> None:
        raw_iid = spec.event.thread_ref.strip()
        if not raw_iid.isdigit():
            msg = f"GitHub PR number is not numeric: {spec.event.thread_ref!r}"
            raise RuntimeError(msg)
        self._host.post_pr_comment(
            repo=spec.event.channel_ref,
            pr_iid=int(raw_iid),
            body=spec.body,
        )


def replier_for(
    source: str,
    *,
    bot: "SlackBotBackend | None" = None,
    gitlab: "GitLabAPI | None" = None,
    github: "GitHubCodeHost | None" = None,
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
