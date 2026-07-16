"""Durable user-facing notice when a self-update / ``t3 update`` skips a clone.

The housekeeping self-update scanner and ``t3 update`` fast-forward each
editable/work-repo clone only when its tree is clean and on the default branch.
A dirty or detached/off-default clone is SKIPPED — correctly, since the safe
primitive never resets/stashes to recover. But the skip was only a ``logger``
line, so the clone went silently stale: ``t3`` kept running the stale editable
code and nobody knew (#2836).

This helper turns that skip into a DURABLE user-facing notice via
:func:`teatree.core.notify.notify_user` — a bot→user Slack DM backed by the
``BotPing`` audit ledger, so it is recorded even when no messaging backend is
configured (a ``NOOP`` audit row). The idempotency key folds in the clone's
HEAD sha so a persistent skip is notified once (not every tick/run), while a
genuinely new stale state (HEAD moved, then re-broke) re-notifies. The notice
NAMES the repo path and the manual remediation — it never suggests, and the
caller never performs, an auto-stash/auto-checkout recovery.
"""

import enum
import logging
from dataclasses import dataclass

from teatree.core.notify import NotifyKind, notify_user
from teatree.core.modelkit.notify_policy import NotifyAudience

logger = logging.getLogger(__name__)


class StaleCloneReason(enum.StrEnum):
    """Why a clone was skipped — drives the remediation line in the notice."""

    DIRTY = "dirty"
    OFF_DEFAULT = "off_default"


@dataclass(frozen=True, slots=True)
class StaleCloneSkip:
    """One skipped clone the self-update / ``t3 update`` could not fast-forward."""

    label: str
    repo_path: str
    reason: StaleCloneReason
    head_sha: str
    default_branch: str = ""
    detail: str = ""


def _idempotency_key(skip: StaleCloneSkip) -> str:
    return f"stale_clone_skip:{skip.label}:{skip.reason.value}:{skip.head_sha[:12]}"


def _remediation(skip: StaleCloneSkip) -> str:
    if skip.reason is StaleCloneReason.DIRTY:
        return "Commit, stash, or revert the tracked change, then re-run `t3 update`."
    branch = skip.default_branch or "main"
    return f"Switch it back and sync: `git switch {branch} && git pull --ff-only`, then re-run `t3 update`."


def stale_clone_message(skip: StaleCloneSkip) -> str:
    """Build the user-facing notice body naming the repo path + remediation."""
    headline = (
        "has uncommitted tracked changes" if skip.reason is StaleCloneReason.DIRTY else "is off its default branch"
    )
    detail_line = f" ({skip.detail})" if skip.detail else ""
    return (
        f"`t3 update` could not fast-forward `{skip.label}` — its clone at "
        f"`{skip.repo_path}` {headline}{detail_line}, so it is STALE behind origin "
        f"and the editable `t3` may be running old code (#2836). The safe "
        f"self-update never resets/stashes to recover. {_remediation(skip)}"
    )


def notify_stale_clone_skip(skip: StaleCloneSkip) -> bool:
    """Emit a durable bot→user notice that *skip*'s clone was skipped as stale.

    Returns whether the notice was delivered (a no-backend environment records a
    durable ``BotPing`` NOOP audit row and returns ``False``). Never raises —
    surfacing a stale clone must not break the update flow it rides on.
    """
    try:
        return notify_user(
            stale_clone_message(skip),
            kind=NotifyKind.INFO,
            idempotency_key=_idempotency_key(skip),
            audience=NotifyAudience.INTERNAL,
        )
    except Exception:
        logger.exception("notify_stale_clone_skip failed for %s (%s)", skip.label, skip.repo_path)
        return False
