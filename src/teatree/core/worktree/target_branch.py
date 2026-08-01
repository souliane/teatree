"""Which branch a shipped PR targets, and which ref the currency gates predict against (#940).

Two forms of one decision, because the two consumers need different spellings:

* :func:`resolve_target_branch` returns a REMOTE-qualified ref (``origin/main``)
  for the merge-prediction gates — :func:`~teatree.core.worktree.branch_currency.sha_conflicts_with_target`
  and friends fetch the remote named by its first segment.
* :func:`resolve_pr_target_branch` returns a BARE branch name for
  :class:`~teatree.core.backend_protocols.PullRequestSpec` — GitLab's
  ``target_branch`` and ``gh pr create --base`` both take a branch, not a
  remote-qualified ref — or ``""`` to let the forge's own default branch stand.

It lives here, next to the currency gate rather than inside the ``pr create``
command package, so every producer can reach it without importing a command
module: the ship runner, the orphan-branch ``ensure-pr`` lane, and the two
``ticket clear`` pre-flights all resolve the target through this one seam.
"""

import re
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.utils import git
from teatree.utils.run import CommandFailedError

if TYPE_CHECKING:
    from teatree.core.models import Ticket

#: A target is already REMOTE-qualified only when it starts with a remote name.
#: The rule "contains a slash" misreads every ordinary prefixed branch name
#: (``chore/x``, ``release/1.2``, ``feat/y``) as remote-qualified and returns it
#: bare, so the gate fetches a remote that does not exist and fails OPEN.
_REMOTE_QUALIFIED_RE = re.compile(r"^(origin|upstream)/")


def qualified_target(branch: str) -> str:
    """``branch`` as a remote-qualified ref — ``origin/<branch>`` unless already one."""
    stripped = branch.strip()
    return stripped if _REMOTE_QUALIFIED_RE.match(stripped) else f"origin/{stripped}"


def bare_target(branch: str) -> str:
    """``branch`` without its remote prefix — the spelling a forge API accepts."""
    return _REMOTE_QUALIFIED_RE.sub("", branch.strip())


def _explicit_target(ticket: "Ticket | None", branch: str) -> str:
    """The bare target named by the ticket or the setting, or ``""`` when neither applies.

    Two tiers, highest first:

    1. ``ticket.extra['target_branch']`` — the per-ticket override a stacked PR
       sets to base on something other than the repo default.
    2. The ``target_branch`` SETTING — a whole line of work stacking onto ONE
       long-lived integration branch, rather than every ticket repeating the
       same override.

    *branch* is the branch being shipped, and exists for the self-target guard:
    the integration branch must NOT target itself, or its own PR is a no-op and
    the currency gate merges it into itself.
    """
    extra = (ticket.extra or {}) if ticket is not None else {}
    explicit = str(extra.get("target_branch") or "").strip()
    if explicit:
        return bare_target(explicit)
    overlay = (ticket.overlay or None) if ticket is not None else None
    configured = str(get_effective_settings(overlay).target_branch or "").strip()
    if configured and bare_target(configured) != bare_target(branch):
        return bare_target(configured)
    return ""


def resolve_pr_target_branch(ticket: "Ticket | None", *, branch: str = "") -> str:
    """The bare branch a shipped PR must target; ``""`` defers to the forge default.

    Deliberately does NOT fall back to the local clone's idea of the default
    branch: an empty spec field lets the backend read the project's real default
    from the forge, which is the authority on it.
    """
    return _explicit_target(ticket, branch)


def resolve_target_branch(ticket: "Ticket | None", repo: str, *, branch: str = "") -> str:
    """The remote-qualified ref the merge-prediction gates fetch and compare against.

    Falls through to ``origin/<default>`` — the historical behaviour, and still
    what an unset setting and an unset ticket override resolve to.
    """
    explicit = _explicit_target(ticket, branch)
    if explicit:
        return qualified_target(explicit)
    try:
        return f"origin/{git.default_branch(repo=repo)}"
    except (CommandFailedError, RuntimeError, ValueError):
        return "origin/main"
