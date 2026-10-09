"""`t3 setup recover-account-switch` — the explicit `/login` recovery surface (#1916).

Runs the same detect-and-invalidate cycle as the `t3 doctor` gate, but as a standalone
command the agent (or user) can invoke on demand after a `/login`, without the rest of
the doctor run.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def recover_account_switch() -> None:
    """Detect a Claude account switch and invalidate the backend and token-health caches."""
    ensure_django()
    from teatree.core.account_switch import detect_and_recover_account_switch  # noqa: PLC0415 — lazy CLI import

    outcome = detect_and_recover_account_switch()
    if not outcome.switched:
        typer.echo(
            f"No account switch since last recovery (active {outcome.current_fingerprint[:8] or '?'}…).",
        )
        return

    typer.echo(
        f"Account switch: {outcome.previous_fingerprint[:8]}… → {outcome.current_fingerprint[:8]}…. "
        f"Backend cache invalidated; token health expired ({outcome.token_health_rows_expired} row(s), "
        "next `t3 tokens` re-probes); new account recorded.",
    )


__all__ = ["recover_account_switch"]
