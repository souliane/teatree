"""Reference-ratchet staleness handler — the executor for ``ratchet.stale_pins`` (#4451).

The scanner (:mod:`teatree.loop.scanners.ratchet_staleness`) only FLAGS that the
core clone's ratchet has pins the scanner no longer reports; this handler surfaces
that to the owner with the exact one-command repair. It changes nothing — no
clone write, no branch, no PR — mirroring ``ci_eval_heal``'s observe-only half.

Idempotent on the stale SET rather than on the tick: the DM key is derived from
the sorted pins, so a condition that persists across ticks notifies once and a
condition that grows notifies again. A messaging failure raises into
``_execute_mechanical``, which records it in the tick's errors.
"""

import hashlib

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify import NotifyKind, notify_user
from teatree.loop.dispatch import ActionPayload

_MAX_LISTED = 10


def _digest(rows: list[list[str]]) -> str:
    return hashlib.sha256("\n".join("\t".join(row) for row in rows).encode()).hexdigest()[:12]


def _render(repo: str, rows: list[list[str]]) -> str:
    listed = rows[:_MAX_LISTED]
    lines = [f"  {ratchet}  {path}  {ref}" for ratchet, path, ref in listed]
    if len(rows) > _MAX_LISTED:
        lines.append(f"  … and {len(rows) - _MAX_LISTED} more")
    return "\n".join(
        [
            f"{len(rows)} stale reference-ratchet pin(s) on {repo} — main's ratchet is loose until they are deleted:",
            *lines,
            "Repair: `t3 tool ratchet-prune --write` (deletes exactly these; it can never add a pin).",
        ]
    )


def report_ratchet_staleness(payload: ActionPayload) -> None:
    """Surface the core clone's stale ratchet pins to the owner; a failure raises into the tick's errors."""
    rows = [list(row) for row in payload.get("stale", [])]
    repo = str(payload.get("repo", "the core clone"))
    if not rows:
        return
    notify_user(
        _render(repo, rows),
        kind=NotifyKind.INFO,
        idempotency_key=f"ratchet-stale-{_digest(rows)}",
        audience=NotifyAudience.OWNER_ESCALATION,
    )


__all__ = ["report_ratchet_staleness"]
