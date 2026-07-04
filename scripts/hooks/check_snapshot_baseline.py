"""Pre-commit hook: refuse a staged visual baseline with no recorded attestation.

A commit that adds or rewrites a Playwright visual-regression baseline (a file
under ``__snapshots__/`` or ``<spec>-snapshots/``) silently redefines what
"looks correct" means. Without a recorded visual verification, a regression
baked into the new reference reads green forever. This gate refuses such a
commit unless the ticket carries a recorded visual-verification attestation —
a green + POSTED :class:`~teatree.core.models.e2e_mandatory_run.E2eMandatoryRun`,
the same evidence the mandatory-E2E gate already consumes (the attestation
"rides the existing e2e-run attestation row").

Enforcement mirrors the §17.6 gate contract:

- vacuous-on-empty — no staged baseline → exit 0 without touching Django/DB.
- kill-switch ``snapshot_baseline_gate_enabled`` disables it, resolved DB-first
through ``get_effective_settings`` (the canonical resolver every sibling gate uses),
so a DB ``config_setting set snapshot_baseline_gate_enabled false`` actuates the
hook — not only a raw ``~/.teatree.toml`` edit.
- never-lockout ``ALLOW_SNAPSHOT_BASELINE='<reason>'`` sanctions one commit.
- crash ≠ deny — a Django/DB error, or a cwd with no resolvable ticket, fails
open with a warning (a gate bug must never wedge commits). Only a resolved
ticket that genuinely lacks the attestation is a BLOCK.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.quality.snapshot_baseline import block_message, snapshot_baselines

if TYPE_CHECKING:
    from teatree.core.models import Ticket

_ALLOW_ENV = "ALLOW_SNAPSHOT_BASELINE"


def _gate_enabled(overlay_name: str | None) -> bool:
    """Resolve ``snapshot_baseline_gate_enabled`` via the canonical DB-first resolver.

    Reads the kill-switch through ``get_effective_settings`` — the same DB-home
    resolver every sibling gate consults (see ``e2e_mandatory_gate``) — so a DB
    ``config_setting set snapshot_baseline_gate_enabled false`` actuates the hook,
    not only a raw ``~/.teatree.toml`` edit. Requires Django, so the caller reads
    it only once a baseline is staged.
    """
    from teatree.config import get_effective_settings

    return bool(get_effective_settings(overlay_name).snapshot_baseline_gate_enabled)


def _staged_paths() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _resolve_ticket() -> "Ticket | None":
    """Read-only resolve the current worktree's ticket, or ``None``.

    A pre-commit hook must never mutate the DB as a side effect, so this uses
    the non-mutating :func:`match_worktree_by_path` (never the auto-registering
    ``resolve_worktree``). ``None`` when no worktree row matches the cwd — the
    caller then fails open rather than blocking an un-attributable commit.
    """
    from teatree.core.resolve import match_worktree_by_path

    cwd = os.environ.get("T3_ORIG_CWD", os.environ.get("PWD", str(Path.cwd())))
    worktree = match_worktree_by_path(cwd)
    return worktree.ticket if worktree is not None else None


def _record_command(ticket_pk: int) -> str:
    return (
        f"t3 <overlay> lifecycle record-e2e-run {ticket_pk} --spec <e2e-spec-path> "
        f"--result green --head-sha <40-char-sha> --posted-url <evidence-url>"
    )


def _decide(baselines: list[str]) -> int:
    """Return the exit code for a commit that staged *baselines* (non-empty).

    Bootstraps Django, resolves the ticket read-only, honours the DB-first
    kill-switch on the ticket's overlay, and consults the visual attestation.
    Any failure to resolve/verify fails OPEN (warn + allow).
    """
    from teatree.utils.django_bootstrap import ensure_django

    ensure_django()
    ticket = _resolve_ticket()
    if ticket is None:
        print(
            "WARN: snapshot-baseline gate — staged visual baseline(s) but no ticket resolves "
            "from this worktree; allowing (cannot bind the attestation).",
            file=sys.stderr,
        )
        return 0
    if not _gate_enabled(ticket.overlay or None):
        return 0

    from teatree.core.models.e2e_mandatory_run import E2eMandatoryRun

    if E2eMandatoryRun.has_visual_verification(ticket):
        return 0
    print(block_message(baselines, ticket_ref=str(ticket.pk), record_command=_record_command(ticket.pk)))
    return 1


def main() -> int:
    # Detect a staged baseline FIRST — cheap and Django-free. No baseline staged
    # (the common commit) exits 0 without paying the Django bootstrap; only a
    # staged baseline pays for the kill-switch + attestation resolution.
    baselines = snapshot_baselines(_staged_paths())
    if not baselines:
        return 0
    marker = os.environ.get(_ALLOW_ENV, "").strip()
    if marker:
        print(f"NOTE: snapshot-baseline gate allowed via {_ALLOW_ENV}={marker!r} — {len(baselines)} baseline(s).")
        return 0
    try:
        return _decide(baselines)
    except Exception as exc:  # noqa: BLE001 — fail-open: a gate bug must never wedge commits repo-wide.
        print(f"WARN: snapshot-baseline gate errored — failing open: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
