"""``_check_*`` probes for teatree's own MCP server and the services it serves, invoked by `t3 doctor check`.

Each helper is narrow (single concern, single ``typer.echo`` path) and returns
``bool`` for pass/fail aggregation by :func:`teatree.cli.doctor.run_checks.run_doctor_checks`.
"""

import shutil
import sys
from collections.abc import Callable

import typer

from teatree.utils.uv_constraints import uv_tool_install_hint


def _check_declared_services_configured() -> bool:
    """Per declaring overlay: FAIL a service no declarer holds credentials for, WARN a declarer served by another.

    Builds each declarer's client with the builders the teatree MCP service tools use. Those
    tools serve a service from its first configured declarer, so a declarer without
    credentials of its own still gets served — with another overlay's account.
    """
    from teatree.mcp.service_resolver import (  # noqa: PLC0415 — deferred: lazy CLI import
        SERVICE_CLIENTS,
        declaring_overlays,
    )

    ok = True
    for service, overlays in declaring_overlays().items():
        client = SERVICE_CLIENTS[service]
        missing = {name: reason for name in overlays if (reason := _missing_client(client.build, name))}
        if len(missing) == len(overlays):
            reasons = "; ".join(f"{name}: {reason}" for name, reason in missing.items())
            typer.echo(
                f"FAIL  {service.value} is declared by {', '.join(overlays)} but none has a configured "
                f"{client.description} ({reasons}). Configure teatree's own credentials for it — the teatree MCP "
                "and the `t3` CLI serve it from those."
            )
            ok = False
        elif missing:
            typer.echo(
                f"WARN  {service.value} is served to {', '.join(missing)} from another declaring overlay's "
                f"{client.description}, so with that overlay's account; configure its own if that is wrong."
            )
    return ok


def _missing_client(build: Callable[[str], object], overlay: str) -> str:
    try:
        return "" if build(overlay) is not None else "no credentials"
    except Exception as exc:  # noqa: BLE001 — one overlay's broken builder must not abort the doctor run
        return f"{exc.__class__.__name__}: {exc}"


def _check_teatree_mcp_registration() -> bool:
    """Verify teatree's own structured-search MCP server is wired (#2863).

    Structural check: confirms the plugin-bundled ``.mcp.json`` still declares
    the ``teatree`` stdio server pointing at ``t3 mcp serve`` (the file the
    repo ships at its root — Claude Code starts plugin-bundled MCP servers
    automatically once the plugin is enabled, so nothing more is required to
    make the tools reachable). Whether the server actually runs is
    :func:`_check_teatree_mcp_liveness`'s verdict.

    A WARN, never a hard FAIL: the resolved clone (the same main-clone
    resolution the plugin registration uses) can legitimately lag a merged
    change until the next ``t3 update`` — that is normal, self-correcting
    operation, not a misconfiguration worth reddening the whole doctor run
    over. Crash-proof: any error also degrades to a WARN.
    """
    from teatree.cli.doctor.plugin_repair import _resolve_main_clone  # noqa: PLC0415 — avoids a doctor-package cycle
    from teatree.core.mcp_registration import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        verify_teatree_mcp_registration,
    )

    try:
        repo = _resolve_main_clone()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Could not resolve the teatree clone to verify .mcp.json: {exc}")
        return True
    if repo is None:
        return True

    outcome = verify_teatree_mcp_registration(repo)
    if not outcome.ok:
        typer.echo(f"WARN  {outcome.message}")
    return True


def _resolve_registered_mcp_command() -> list[str] | None:
    """The argv the ``teatree`` MCP registration actually launches, or ``None``.

    ``.mcp.json`` declares ``t3 mcp serve`` as a PATH lookup, so this exercises what a
    client exercises — the console script on PATH, not this process's own entry point,
    which on a container-wrapped install is a different program.
    """
    t3_bin = shutil.which("t3")
    if t3_bin is None:
        return None
    return [t3_bin, "mcp", "serve"]


def _check_version_skew(*, repair: bool) -> bool:
    """FAIL on declared-versus-installed drift in the env that runs ``t3`` (#4049).

    True when there is nothing to report — including when there is no editable source
    tree to measure against, which is an absent question rather than a clean answer.
    """
    from teatree.cli.doctor.skew_repair import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        repair_version_skew,
        report_version_skew,
    )
    from teatree.mcp.liveness import skew_finding  # noqa: PLC0415 — deferred: keeps CLI startup light

    try:
        from teatree.utils.dep_skew import running_env_skew  # noqa: PLC0415 — deferred: keeps CLI startup light
    except ModuleNotFoundError as exc:
        # The skew check is the ONE check that needs a non-stdlib import (``packaging``,
        # for real specifier semantics). The env it measures is the env it runs in, so the
        # exact breakage it exists to catch can take it out first — and, uncaught, it took
        # the WHOLE doctor run with it. A check that cannot run is an absent answer, not a
        # clean one: WARN naming the module so the next run's DM carries the cause.
        repair_command = uv_tool_install_hint("uv tool install --editable . --overrides uv-overrides.txt --reinstall")
        typer.echo(
            f"WARN  the declared-versus-installed skew check could not run: {exc.__class__.__name__}: {exc}. "
            f"Reinstall the running env (`{repair_command}`) "
            "to restore it."
        )
        return True

    measured = running_env_skew()
    if measured is None:
        return True
    source, skews = measured
    if not skews:
        return True
    typer.echo(f"FAIL  {skew_finding([skew.summary for skew in skews], source=source / 'pyproject.toml')}")
    if repair:
        return repair_version_skew(source, skews)
    report_version_skew(skews)
    return False


def _check_teatree_mcp_liveness(*, repair: bool = False) -> bool:
    """EXERCISE teatree's own MCP server — hard FAIL when it is not usable (#4049).

    The counterpart to :func:`_check_teatree_mcp_registration`, which is structural and
    a WARN by design. This one spawns the registered ``t3 mcp serve``, speaks a real
    ``initialize`` frame to it over stdio, and keeps the stderr ``claude mcp list``
    throws away — so an enabled-but-dead server is a FAIL that names its cause and the
    exact remedy instead of one advisory line among twenty.

    Declared-versus-installed skew is checked first, because that cause kills the server
    before it can report anything about itself. It is REPAIRED only under ``repair`` —
    the flag every other mutating check threads — because a reinstall replaces the
    console script the calling session is running under, which a read-only
    ``t3 doctor check`` must never do; without it the skew is reported with the exact
    command that clears it.

    Degrades to a WARN only when the check cannot RUN at all (no ``t3`` on PATH, an
    unresolvable source tree) — never for a server that ran and failed.
    """
    from teatree.mcp.liveness import exercise_mcp_server  # noqa: PLC0415 — deferred: keeps CLI startup light

    ok = _check_version_skew(repair=repair)

    argv = _resolve_registered_mcp_command()
    if argv is None:
        typer.echo("WARN  `t3` is not on PATH, so the registered `t3 mcp serve` could not be exercised.")
        return ok

    try:
        outcome = exercise_mcp_server(argv)
    except OSError as exc:
        typer.echo(f"WARN  Could not spawn `{' '.join(argv)}` to exercise the MCP server: {exc}")
        return ok

    if outcome.slow:
        typer.echo(f"WARN  {outcome.finding}")
        return ok
    if outcome.ok:
        typer.echo(f"OK    {outcome.finding}")
        return ok
    typer.echo(f"FAIL  {outcome.finding}")
    if outcome.stderr_excerpt:
        typer.echo("      Captured stderr (`claude mcp list` never shows this):")
        for line in outcome.stderr_excerpt.splitlines():
            typer.echo(f"        {line}")
    typer.echo(f"      Reproduce: `{' '.join(argv)}` — this ran as {sys.executable}.")
    return False
