"""Config-read helpers the scanner factories consume.

The pure ``~/.teatree.toml`` / env readers that the ``_*_scanner_for`` builders
in :mod:`teatree.loop.scanner_factories` need — resolving per-overlay Slack id,
identity aliases, and the GitLab-approval feature flag. Kept apart from the
scanner-construction concern so ``scanner_factories`` stays under the
module-health LOC cap; re-exported there so existing import sites are unchanged.
"""

import logging
import os
import tomllib
from pathlib import Path

from teatree.config import get_effective_settings

logger = logging.getLogger(__name__)


def _gitlab_approvals_enabled() -> bool:
    """Read the ``TEATREE_GITLAB_APPROVAL_SCANNER_ENABLED`` feature flag.

    Default off — the scanner is poll-driven and overlaps with the webhook
    path; deployments that already wire ``/hooks/gitlab/`` do not need it.
    Returns True for any truthy value (``1``, ``true``, ``yes``,
    case-insensitive); anything else (unset, ``0``, ``false``) is off.
    """
    raw = os.environ.get("TEATREE_GITLAB_APPROVAL_SCANNER_ENABLED", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _user_slack_id_for_overlay(overlay_name: str) -> str:
    """Resolve ``slack_user_id`` for the active overlay (overlay → global → empty).

    Used by :class:`ReviewNagScanner` to know where to DM long-stale MR
    warnings. Reads ``~/.teatree.toml`` directly so a fresh tick picks up
    a runtime config change without requiring an overlay reload.
    """
    try:
        toml_path = Path.home() / ".teatree.toml"
        if not toml_path.is_file():
            return ""
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    overlays = data.get("overlays") or {}
    if overlay_name and isinstance(overlays.get(overlay_name), dict):
        user_id = overlays[overlay_name].get("slack_user_id", "")
        if user_id:
            return str(user_id)
    teatree_cfg = data.get("teatree") or {}
    return str(teatree_cfg.get("slack_user_id", ""))


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
