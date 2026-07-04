"""No-bulk-close deterministic gate (PR-08, folds #1931).

The post-mortem finding this converts into a deterministic gate: an agent (or a
command) closing a large batch of tickets/MRs in one action is a recurring way
work is lost — a mis-scoped sweep mass-closes issues that were not actually
resolved. The prior guard was a prose ASK-GATE directive stamped onto the
backlog-sweep task (:mod:`teatree.loop.scanners.backlog_sweep`), which an agent
could forget under load.

This module is that gate's deterministic core, and both close paths route
through it: the ``t3 <overlay> ticket bulk-close`` command
(:mod:`teatree.core.management.commands._close_commands`) calls it directly, and
the backlog-sweep directive now instructs the dispatched skill to perform its
auto-closes through that same ``ticket bulk-close`` command — never a raw
per-item ``ticket ignore`` loop — so an over-threshold autonomous close is
refused the same as a manual one. The residual (an agent must invoke the gated
command) is the CLI's own boundary, not a hole this gate can close on its own.

The rule: a single close action over more than ``bulk_close_threshold`` items is
refused unless the caller supplies an explicit per-item confirmation token for
**every** item. The token is the item's own identifier — so confirming a bulk
close means typing out each id, never a blanket "close all". A close of
``≤ threshold`` items is always allowed (the threshold is the batch size a human
can reasonably eyeball).

The gate is a pure function over its inputs — no network, no clock, no DB — so
it is trivially testable and deterministic. It returns a non-empty refusal
string (``""`` = allowed), the same shape the review pre-publish gates use.
"""

from collections.abc import Iterable

from teatree.config import get_effective_settings

_PREVIEW_CAP = 10


def _clean(items: Iterable[object]) -> list[str]:
    """Stripped, non-blank string forms preserving order (duplicates kept)."""
    return [s for s in (str(item).strip() for item in items) if s]


def bulk_close_threshold() -> int:
    """The configured batch-size ceiling below which a close needs no tokens."""
    return get_effective_settings().bulk_close_threshold


def check_bulk_close(
    *,
    items: Iterable[object],
    confirmed_tokens: Iterable[object],
    threshold: int | None = None,
) -> str:
    """Return a non-empty refusal when a bulk close lacks per-item confirmation.

    *items* are the ticket/MR identifiers a single action would close;
    *confirmed_tokens* are the explicit per-item confirmation tokens supplied
    (each token is an item's own identifier). *threshold* defaults to
    ``bulk_close_threshold`` — pass it explicitly only in tests.

    Returns ``""`` (proceed) when the batch is ``≤ threshold`` items, or when
    every item has a matching confirmation token. Otherwise returns a refusal
    naming the batch size, the threshold, and the items still un-confirmed.
    """
    resolved = bulk_close_threshold() if threshold is None else threshold
    targets = _clean(items)
    if len(targets) <= resolved:
        return ""
    confirmed = set(_clean(confirmed_tokens))
    unconfirmed = [target for target in targets if target not in confirmed]
    if not unconfirmed:
        return ""
    preview = ", ".join(unconfirmed[:_PREVIEW_CAP]) + (" ..." if len(unconfirmed) > _PREVIEW_CAP else "")
    return (
        f"Refusing bulk close of {len(targets)} items (threshold {resolved}): a close of more than "
        f"{resolved} tickets/MRs at once requires an explicit per-item confirmation token for EACH item, "
        f"so a mis-scoped sweep cannot mass-close silently. {len(unconfirmed)} item(s) are un-confirmed: "
        f"{preview}. Re-run echoing every id in `--confirm <comma,separated,ids>`, or close "
        f"them in batches of {resolved} or fewer."
    )
