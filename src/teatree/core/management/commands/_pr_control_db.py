"""``pr`` control-DB topology concern (split out of ``pr.py``).

The canonical control DB lives in the ``teatree_control_db`` named volume, which
has no host path at all, so a host process cannot open it — ever. That is the
deployment shape, not a misconfiguration to repair. Every ``pr`` path past its
own preconditions needs the ORM, so a host invocation used to die on a raw
``OperationalError`` naming neither the cause nor the remedy; the rational next
move for an agent reading that is a raw ``gh pr create``, which is exactly the
workaround the "fix ``t3``, never hand-roll around it" rule forbids (#4170).

Kept as a sibling module (same pattern as ``_pr_ticket_resolve.py`` /
``_pr_preview.py``) so the topology precondition is named by its own file and
``pr.py`` stays within the module-health LOC budget.
"""

import os
from pathlib import Path
from typing import TypedDict

from django.conf import settings

from teatree.db.boundary import control_db_unreachable_reason


class ControlDbUnreachableError(TypedDict):
    """``pr create`` refused before touching the ORM because the control DB is out of reach."""

    error: str
    hint: str


def configured_db_path() -> Path:
    """The database THIS process would open — the subject of the topology question.

    Read from the resolved settings rather than from the canonical location, so the
    answer is about the database actually in play: a test database, a per-worktree
    isolated copy, and the canonical control DB are three different subjects and only
    the last one sits behind a container-only mount.
    """
    return Path(str(settings.DATABASES["default"]["NAME"]))


def unreachable_control_db_reason() -> str | None:
    """Why this process cannot reach the DB it is configured for, or ``None`` when it can."""
    return control_db_unreachable_reason(configured_db_path(), env=os.environ)


def control_db_unreachable_error(reason: str) -> ControlDbUnreachableError:
    """The ``pr create`` refusal for an unreachable control DB — a remedy, not a diagnosis.

    Worded so it cannot be mistaken for the OTHER refusal this command produces: a
    missing ``Ticket`` row is a state fact with a state remedy, while this is a
    topology fact whose only remedy is running the command where the volume is mounted.
    """
    return ControlDbUnreachableError(
        error=(
            f"`pr create` cannot open the control DB: {reason}. Nothing on the host can reach it, "
            f"so re-run this inside the worker container — never fall back to a raw `gh pr create`, "
            f"which bypasses the ship gates, the FSM transition and the on-behalf gates."
        ),
        hint="deploy/t3 <overlay> pr create <ticket-id> (the `t3` shell alias already points there)",
    )
