"""Poll-driven GitHub events scanner — the fallback complement to the App webhook (#4795).

Mirrors :class:`~teatree.loop.scanners.gitlab_approvals.GitLabApprovalsScanner`'s
role: when the ``polling`` transport preset is active
(:func:`teatree.loop.scanner_factory_config._github_polling_enabled`), this
scanner walks each installation's confirmed
(:attr:`~teatree.core.models.github_app.GitHubAppInstallation.active_repositories`)
repos and submits newly-discovered pull-request and issue updates to the SAME
shared receiver the webhook view uses
(:func:`teatree.core.github_app.webhook_normalize.normalize` +
:func:`teatree.core.views._webhook_persistence.persist_incoming_event`) — so a
poll discovery and a webhook delivery of the same logical update collapse onto
one :class:`~teatree.core.models.incoming_event.IncomingEvent` row via the
shared :mod:`teatree.core.github_app.event_identity`.

Scoped to the ``pull_request`` and ``issues`` families for this scanner (the
webhook path already covers every subscribed family; polling exists as a
fallback for reachability, not a second webhook) — comments, pushes, and
checks are webhook-only until a later pass extends this list. This scanner
performs NO merge/review/dispatch action itself, matching the #4795 AC that
webhook and polling "cannot directly execute merge, review, dispatch, or
ticket actions" — :class:`~teatree.loop.scanners.incoming_events.IncomingEventsScanner`
is the existing, source-agnostic consumer that turns a persisted
``IncomingEvent`` into dispatch-worthy work, on its own later tick.
"""

import logging
from dataclasses import dataclass

from teatree.backends.github.api import _gh_api_get
from teatree.backends.github.app_auth import InstallationTokenCache
from teatree.core.github_app.webhook_normalize import normalize
from teatree.core.models import GitHubAppInstallation, GitHubPollCursor
from teatree.core.views._webhook_persistence import persist_incoming_event
from teatree.loop.scanners.base import ScanSignal
from teatree.types import RawAPIDict
from teatree.utils.secrets import SecretNotFoundError, read_pass_required

logger = logging.getLogger(__name__)

_PRIVATE_KEY_PASS_KEY = "github-app/private-key"  # noqa: S105 — a `pass` key NAME, not a credential


@dataclass(frozen=True, slots=True)
class _FamilyPoll:
    event_type: str
    endpoint_template: str
    wrap_key: str
    synthetic_action: str


_FAMILIES: tuple[_FamilyPoll, ...] = (
    _FamilyPoll(
        event_type="pull_request",
        endpoint_template="repos/{repo}/pulls?state=all&sort=updated&direction=desc",
        wrap_key="pull_request",
        synthetic_action="synchronize",
    ),
    _FamilyPoll(
        event_type="issues",
        endpoint_template="repos/{repo}/issues?state=all&sort=updated&direction=desc",
        wrap_key="issue",
        synthetic_action="edited",
    ),
)


def _default_token_cache(app_id: int) -> InstallationTokenCache | None:
    try:
        pem = read_pass_required(_PRIVATE_KEY_PASS_KEY)
    except SecretNotFoundError:
        logger.warning("github_polling: no App private key in `pass` — skipping this tick")
        return None
    return InstallationTokenCache(app_id=str(app_id), private_key_pem=pem)


@dataclass(slots=True)
class GitHubPollingScanner:
    """Poll every active, un-suspended installation's confirmed repos for new PR/issue updates.

    ``token_cache`` is injectable for tests; production uses
    :func:`_default_token_cache`, resolved per-installation (from its own
    ``app_id``) since an operator could in principle register more than one App.
    """

    token_cache: InstallationTokenCache | None = None
    name: str = "github_polling"

    def scan(self) -> list[ScanSignal]:
        for installation in GitHubAppInstallation.objects.filter(suspended_at__isnull=True):
            self._poll_installation(installation)
        return []

    def _poll_installation(self, installation: GitHubAppInstallation) -> None:
        cache = self.token_cache or _default_token_cache(installation.app_id)
        if cache is None:
            return
        try:
            token = cache.token_for(installation.installation_id)
        except Exception:
            logger.exception("github_polling: could not mint a token for installation %s", installation.installation_id)
            return
        for repo in installation.active_repositories:
            if not isinstance(repo, str) or not repo:
                continue
            for family in _FAMILIES:
                self._poll_family(installation, repo, family, token)

    def _poll_family(self, installation: GitHubAppInstallation, repo: str, family: _FamilyPoll, token: str) -> None:
        cursor = GitHubPollCursor.cursor_for(
            installation=installation, repo_full_name=repo, event_family=family.event_type
        )
        try:
            items = _gh_api_get(family.endpoint_template.format(repo=repo), token=token)
        except Exception:
            logger.exception("github_polling: %s poll failed for %s", family.event_type, repo)
            return
        if not isinstance(items, list):
            return
        newest = cursor
        for item in items:
            if not isinstance(item, dict):
                continue
            if family.event_type == "issues" and "pull_request" in item:
                continue  # GitHub's issues endpoint also lists PRs — the pull_request family covers those.
            updated_at = item.get("updated_at")
            if not isinstance(updated_at, str) or not updated_at:
                continue
            if cursor and updated_at <= cursor:
                break  # sorted newest-first: everything after this is already seen.
            if not newest or updated_at > newest:
                newest = updated_at
            self._persist(repo, family, item)
        if newest and newest != cursor:
            GitHubPollCursor.advance(
                installation=installation, repo_full_name=repo, event_family=family.event_type, cursor_value=newest
            )

    @staticmethod
    def _persist(repo: str, family: _FamilyPoll, item: RawAPIDict) -> None:
        envelope: RawAPIDict = {
            "repository": {"full_name": repo},
            family.wrap_key: item,
            "action": family.synthetic_action,
        }
        persist_incoming_event(normalize(family.event_type, envelope))


__all__ = ["GitHubPollingScanner"]
