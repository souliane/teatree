"""Config-read helpers the scanner factories consume.

The pure DB config / env readers that the ``_*_scanner_for`` builders
in :mod:`teatree.loop.scanner_factories` need — resolving per-overlay Slack id,
identity aliases, and the GitLab-approval feature flag. Kept apart from the
scanner-construction concern so ``scanner_factories`` stays under the
module-health LOC cap; re-exported there so existing import sites are unchanged.
"""

import logging

from teatree.config import get_effective_settings

logger = logging.getLogger(__name__)


def stranger_pr_admission(overlay_name: str) -> tuple[tuple[str, ...], str]:
    """The ``(trusted_authors, admit_label)`` pair arming the #3634 stranger-PR gate.

    The same two values the intake scanner is built with, so "who may the factory
    work for" is answered identically on the issue and PR sides.
    """
    from teatree.config import effective_trusted_issue_authors  # noqa: PLC0415 — deferred: loaded at tick time
    from teatree.core.intake.factory_admission import DEFAULT_ADMIT_LABEL  # noqa: PLC0415 — deferred leaf import

    settings = get_effective_settings(overlay_name or None)
    return (
        tuple(sorted(effective_trusted_issue_authors(settings))),
        settings.issue_implementer_label or DEFAULT_ADMIT_LABEL,
    )


def _gitlab_approvals_enabled() -> bool:
    """Resolve the GitLab-approval poll-scanner feature flag.

    DB-home (#1775): resolved via the effective-settings tier — an overlay-scoped
    ``ConfigSetting`` row wins over the global one. Default off — the scanner is
    poll-driven and overlaps with the webhook path; deployments that already wire
    ``/hooks/gitlab/`` do not need it. Set via
    ``t3 <overlay> config_setting set gitlab_approval_scanner_enabled true``.
    """
    try:
        return get_effective_settings().gitlab_approval_scanner_enabled
    except Exception:  # noqa: BLE001 — never break a tick on a config read.
        logger.warning("Failed to resolve gitlab_approval_scanner_enabled; defaulting to off")
        return False


def _user_slack_id_for_overlay(overlay_name: str) -> str:
    """Resolve ``slack_user_id`` for the active overlay (overlay → global → empty).

    Used by :class:`ReviewNagScanner` to know where to DM long-stale MR
    warnings. Reads the DB overlays registry + ``ConfigSetting`` store directly
    so a fresh tick picks up a runtime config change without an overlay reload.
    """
    from teatree.config import cold_reader, load_config  # noqa: PLC0415 — deferred: loaded at tick time, not import

    overlays = load_config().raw.get("overlays") or {}
    if overlay_name and isinstance(overlays.get(overlay_name), dict):
        user_id = overlays[overlay_name].get("slack_user_id", "")
        if user_id:
            return str(user_id)
    return cold_reader.str_setting("slack_user_id", default="")


def _user_identity_aliases_for_overlay(overlay_name: str) -> tuple[str, ...]:
    """Resolve ``user_identity_aliases`` honouring any per-overlay override.

    DB-home (#1775): resolved via the effective-settings tier for the named
    overlay — an overlay-scoped ``ConfigSetting`` row wins over the global one;
    with no row anywhere we return the empty tuple so the disposition scanner
    keeps its legacy behaviour.
    """
    try:
        return tuple(get_effective_settings(overlay_name or None).user_identity_aliases)
    except Exception:  # noqa: BLE001 — never break a tick on a config read.
        logger.warning("Failed to resolve user_identity_aliases for %r; defaulting to empty", overlay_name)
        return ()
