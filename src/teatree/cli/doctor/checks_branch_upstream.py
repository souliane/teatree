"""A branch must track its own remote ref, or nothing at all (#4225).

FAIL rather than WARN, deliberately. The hazard is that the only thing between a
routine ``git push`` on such a branch and a push aimed at ``main`` is
``push.default``'s unchosen ``simple`` default — a single layer, under a config
some tooling flips. A box already carrying a score of benign WARNs cannot carry
that as one more: a signal indistinguishable from noise is not a signal.
"""

import typer


def check_branch_upstreams() -> bool:
    """FAIL on any branch whose upstream merge ref is not its own, naming the per-branch remedy.

    An unreadable clone set (no DB, a migration mid-flight) WARNs rather than
    failing: this reports on state it reads, so being unable to read it is
    "unverified", never "broken".
    """
    from teatree.core.worktree.branch_upstream import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        scan_clones,
    )

    try:
        mistracked = scan_clones()
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Branch upstreams UNVERIFIED: the clone set could not be read ({exc}).")
        return True
    if not mistracked:
        return True
    findings = [line for clone in mistracked for line in clone.findings()]
    typer.echo(
        f"FAIL  {len(findings)} branch(es) track an upstream that is not their own — a `git push` on them "
        "aims at someone else's branch under push.default=upstream, and refuses confusingly under the "
        "unchosen `simple` default. Repair every one with: t3 <overlay> workspace repair-branch-upstreams"
    )
    for line in findings:
        typer.echo(f"      {line}")
    return False


__all__ = ["check_branch_upstreams"]
