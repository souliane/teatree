"""Cached CI OAuth fleet health: a spent pool is a broken eval lane, not green."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

import typer

from teatree.backends.github.ci_eval_client import DEFAULT_CI_EVAL_REPO

if TYPE_CHECKING:
    from teatree.token_rows import TokenAccountRow


def check_ci_oauth_pool(*, rows: Sequence["TokenAccountRow"] | None = None) -> bool:
    """FAIL only on measured fleet-wide near exhaustion; unknown readings WARN."""
    from teatree.credential_config import TokenKind  # noqa: PLC0415 — defer CLI boot dependency
    from teatree.token_rows import TokenSource, TokenStatus  # noqa: PLC0415 — Django app registry

    check_membership = rows is None
    if rows is None:
        from teatree.token_report import TokenReport  # noqa: PLC0415 — DB/app-registry dependency at call time

        try:
            rows = TokenReport(from_cache=True).rows()
        except Exception as exc:  # noqa: BLE001 — doctor must keep checking other faults
            typer.echo(f"WARN  Could not read cached CI OAuth pool health: {type(exc).__name__}: {exc}")
            return True
    oauth = [row for row in rows if row.kind is TokenKind.OAUTH and row.source is TokenSource.STORE]
    if not oauth:
        return True
    membership_ok = True
    if check_membership:
        from teatree.ci_oauth_switch import CiAccountSwitcher  # noqa: PLC0415 — doctor-time dependency

        membership_ok = check_ci_oauth_pool_membership(
            oauth, CiAccountSwitcher(repo=DEFAULT_CI_EVAL_REPO).active_pool_accounts()
        )
    unknown = [row.account for row in oauth if row.status in {TokenStatus.UNCACHED, TokenStatus.UNREACHABLE}]
    if unknown:
        typer.echo(f"WARN  CI OAuth pool health unknown for {', '.join(unknown)}; run `t3 tokens` to refresh it.")
    measured = [row for row in oauth if row.status.is_measured]
    if (
        measured
        and len(measured) == len(oauth)
        and all(row.status in {TokenStatus.WARNING, TokenStatus.EXHAUSTED} for row in measured)
    ):
        names = ", ".join(row.account for row in oauth)
        typer.echo(
            f"FAIL  Every configured CI OAuth account is exhausted or near-exhausted: {names}. "
            "The EVAL_OAUTH_TOKENS pool cannot supply a reliable eval run; inspect `t3 tokens` "
            "and reconcile the pool before trusting CI evals."
        )
        return False
    return membership_ok


def check_ci_oauth_pool_membership(rows: Sequence["TokenAccountRow"], pool_accounts: tuple[str, ...] | None) -> bool:
    """Fail if the readable CI receipt differs from configured OAuth entries."""
    from teatree.credential_config import TokenKind  # noqa: PLC0415 — defer CLI boot dependency
    from teatree.token_rows import TokenSource  # noqa: PLC0415 — Django app registry

    configured = {row.account for row in rows if row.kind is TokenKind.OAUTH and row.source is TokenSource.STORE}
    if not configured:
        return True
    if pool_accounts is None:
        typer.echo(
            "FAIL  CI OAuth pool membership is unverified: EVAL_OAUTH_POOL_ACCOUNTS is unreadable or absent. "
            "The hourly housekeeping reconciler must run successfully."
        )
        return False
    active = set(pool_accounts)
    missing = sorted(configured - active)
    stale = sorted(active - configured)
    if missing or stale:
        typer.echo(
            f"FAIL  CI OAuth pool membership drift: missing={missing}, stale={stale}. "
            "The hourly housekeeping reconciler must update EVAL_OAUTH_TOKENS."
        )
        return False
    return True


__all__ = ["check_ci_oauth_pool", "check_ci_oauth_pool_membership"]
