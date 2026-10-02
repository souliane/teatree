"""`t3 doctor` check — every repo the PR sweep sweeps must resolve to a forge (#72).

A bare ``owner/repo`` slug carries no host, so the sweep asks the SCOPE
declarations (``owned_repos``) which forge hosts it. A slug no declaration answers
for is a repo the sweep refuses to touch — and it refuses once per tick, forever,
while the tick report, the merge log and the sweep's own skip surface all read
healthy. That is the shape this fork ran on: the sweep reported success having
enumerated nothing.

The runtime signal is a ``ScannerError`` the dispatcher DMs. This check is the
static half — it names the misconfigured repo before a sweep ever runs.
"""

import typer


def _unroutable_sweep_repos() -> list[tuple[str, str]]:
    """``(overlay, slug)`` for every swept repo whose forge no declaration names."""
    from teatree.core.merge.host_kind import forge_for_repo_slug  # noqa: PLC0415 — deferred: ORM-reading import
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: entry-point discovery

    unroutable: list[tuple[str, str]] = []
    for name, overlay in get_all_overlays().items():
        for slug in overlay.metadata.get_followup_repos():
            try:
                forge = forge_for_repo_slug(slug)
            except Exception as exc:  # noqa: BLE001 — an ambiguous or broken declaration is itself unroutable
                unroutable.append((name, f"{slug} ({exc.__class__.__name__})"))
                continue
            if not forge:
                unroutable.append((name, slug))
    return unroutable


def _check_sweep_repos_resolve_a_forge() -> bool:
    """FAIL naming every swept repo the sweep cannot pick a transport for.

    Crash-proof: any error degrades to one WARN line and a PASS, so a broken probe
    cannot hide the rest of the run.
    """
    try:
        unroutable = _unroutable_sweep_repos()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  PR-sweep forge-routing check crashed: {exc.__class__.__name__}: {exc}")
        return True
    for overlay, slug in unroutable:
        typer.echo(f"FAIL  PR sweep cannot route {slug} [overlay={overlay}] — no declared forge host")
    if unroutable:
        typer.echo(
            "FAIL  Declare the namespace's forge host in the owning overlay's `owned_repos` "
            "(e.g. {'gitlab.com': ['<namespace>']}); until then the sweep refuses that repo every tick."
        )
    return not unroutable
