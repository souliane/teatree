"""``_check_*`` probes for MCP / connector wiring invoked by `t3 doctor check`.

Each helper is narrow (single concern, single ``typer.echo`` path) and returns
``bool`` for pass/fail aggregation by :func:`teatree.cli.doctor.app.run_doctor_checks`.
"""

import os
import shutil
import sys
from pathlib import Path

import typer

_CHROME_DEVTOOLS_MCP_NAME = "chrome-devtools"

#: Set on the child of a skew repair this check performed, so a repair that does not
#: actually clear the skew reports itself instead of reinstalling on every run.
_SKEW_REPAIR_ATTEMPTED_ENV = "_T3_MCP_SKEW_REPAIR_ATTEMPTED"


def _check_chrome_devtools_mcp_suggestion(*, home: Path | None = None, cwd: Path | None = None) -> bool:
    """INFO-suggest the OPTIONAL chrome-devtools MCP e2e aid when it is absent (#3271).

    chrome-devtools MCP gives an interactive DOM/console/network view that makes
    authoring and debugging Playwright e2e specs far more tractable. It is a
    pure developer-experience recommendation — teatree's runtime requires zero
    MCP — so this is an ``INFO`` suggestion, never a ``WARN``/``FAIL``, and its
    absence gates nothing (always returns ``True``). Silent when it is already
    configured. Crash-proof: any read error degrades to a silent pass.
    """
    try:
        from teatree.core.mcp_connectivity import read_enabled_mcp_servers  # noqa: PLC0415 — deferred: light import

        names = {server.name for server in read_enabled_mcp_servers(home=home, cwd=cwd)}
    except Exception:  # noqa: BLE001 — an optional suggestion must never crash or gate the doctor run
        return True
    if _CHROME_DEVTOOLS_MCP_NAME in names:
        return True
    from teatree.core.evidence.browser_diagnosis import (  # noqa: PLC0415 — deferred: light import
        chrome_devtools_add_command,
    )

    typer.echo(
        "INFO  chrome-devtools MCP is an OPTIONAL aid for authoring/debugging Playwright "
        "e2e specs (live DOM, console, network, screenshots). Enable it with "
        f"`{chrome_devtools_add_command()}` (needs a "
        "Chrome executable). It is never required — its absence gates nothing."
    )
    return True


def _check_mcp_connectivity() -> bool:
    """Verify every enabled MCP server is connected + matches its provider (#2282).

    Enumerates the enabled configured MCP servers (``~/.claude.json`` minus the
    per-project disabled set), live-probes each one's connection via
    ``claude mcp list``, and validates each resolves to its overlay-declared
    provider. An enabled-but-disconnected server, or a provider mismatch, is a
    hard FAIL naming the server + a reconnect hint. A probe that cannot run
    (``claude`` absent) degrades to a WARN. Crash-proof: any error degrades to a
    WARN so a doctor run never aborts on this check.
    """
    try:
        from teatree.core.mcp_connectivity import check_mcp_connectivity  # noqa: PLC0415 — deferred: lazy CLI import

        outcome = check_mcp_connectivity()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  MCP connectivity check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if outcome.degraded:
        for finding in outcome.findings:
            typer.echo(f"WARN  {finding}")
        return True
    if outcome.ok:
        return True
    for finding in outcome.findings:
        typer.echo(f"FAIL  {finding}")
    return False


def _check_connector_manifest() -> bool:
    """Verify every overlay-declared claude.ai connector is connected (PR-19).

    Reads each registered overlay's connector manifest and live-probes each
    declared connector. A REQUIRED connector that is down is a hard FAIL with
    mode-correct guidance — first-install (add it in claude.ai Settings →
    Connectors) vs post-account-switch (reconnect it) — followed by the
    ``RECONNECT`` lines. An OPTIONAL down connector is a WARN. A probe that
    cannot run degrades to a WARN. Crash-proof: any error degrades to a WARN so a
    doctor run never aborts on this check.
    """
    try:
        from teatree.core.connector_manifest import (  # noqa: PLC0415 — deferred post-bootstrap: walks overlays + probes MCP
            check_connector_manifest,
        )

        outcome = check_connector_manifest()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Connector-manifest check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if outcome.degraded:
        for finding in outcome.probe_findings:
            typer.echo(f"WARN  {finding}")
        return True
    for finding in outcome.optional_findings:
        typer.echo(f"WARN  {finding}")
    if outcome.ok:
        return True
    for finding in outcome.required_findings:
        typer.echo(f"FAIL  {finding}")
    for line in outcome.reconnect_lines():
        typer.echo(f"      {line}")
    return False


def _check_teatree_mcp_registration() -> bool:
    """Verify teatree's own structured-search MCP server is wired (#2863).

    Structural check: confirms the plugin-bundled ``.mcp.json`` still declares
    the ``teatree`` stdio server pointing at ``t3 mcp serve`` (the file the
    repo ships at its root — Claude Code starts plugin-bundled MCP servers
    automatically once the plugin is enabled, so nothing more is required to
    make the tools reachable). When ``claude`` is on PATH, also live-probes
    visibility via ``claude mcp list``.

    A WARN, never a hard FAIL: the resolved clone (the same main-clone
    resolution the plugin registration uses) can legitimately lag a merged
    change until the next ``t3 update`` — that is normal, self-correcting
    operation, not a misconfiguration worth reddening the whole doctor run
    over. Crash-proof: any error also degrades to a WARN.
    """
    from teatree.cli.doctor.plugin_repair import _resolve_main_clone  # noqa: PLC0415 — avoids a doctor-package cycle
    from teatree.core.mcp_registration import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        TEATREE_MCP_SERVER_NAME,
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

    try:
        from teatree.core.mcp_connectivity import probe_mcp_servers  # noqa: PLC0415 — deferred: keeps CLI startup light

        statuses = probe_mcp_servers()
    except Exception:  # noqa: BLE001 — live probe is best-effort; claude may be absent
        return True
    # #3255: the same shipped ``.mcp.json`` surfaces under two CC scopes on a
    # dogfooding box — ``plugin:t3:teatree`` (plugin scope, the live one) and a
    # separate ``teatree`` (project scope, often Pending approval). Treat any
    # ``:teatree``-suffixed or bare ``teatree`` entry as the same server; WARN
    # only when NONE of them is connected (a genuine disconnection), never when
    # the plugin-scoped one is up beside a pending project entry.
    teatree_statuses = [
        status
        for status in statuses
        if status.name == TEATREE_MCP_SERVER_NAME or status.name.endswith(f":{TEATREE_MCP_SERVER_NAME}")
    ]
    if teatree_statuses and not any(status.connected for status in teatree_statuses):
        typer.echo(
            f"WARN  MCP server '{TEATREE_MCP_SERVER_NAME}' is registered but reports NOT "
            "connected in `claude mcp list` — it may not have started for this session yet. "
            "The authoritative verdict is the exercising check below, which spawns the "
            "server and says WHY.",
        )
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


def _repair_version_skew(source: Path) -> bool:
    """Reinstall the running env to clear declared-versus-installed skew (#4049).

    Returns whether the skew is gone afterwards. A stale env is a MECHANICAL cause with
    a deterministic fix, so it is repaired rather than escalated — the operator is
    interrupted only for the causes that need judgement. Guarded by
    :data:`_SKEW_REPAIR_ATTEMPTED_ENV` so a repair that does not take reports itself
    instead of reinstalling on every doctor run.
    """
    from teatree.cli.dep_drift_repair import (  # noqa: PLC0415 — deferred: repair path only
        RepairPlan,
        resolve_repair_plan,
    )
    from teatree.utils.dep_skew import find_version_skew  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415 — deferred: repair path only

    if os.environ.get(_SKEW_REPAIR_ATTEMPTED_ENV):
        typer.echo("      Repair already attempted this run and the skew persists — fix it by hand.")
        return False
    plan = resolve_repair_plan(["<version skew>"])
    if not isinstance(plan, RepairPlan):
        typer.echo(f"      Cannot self-repair: {plan}")
        return False

    typer.echo(f"      Self-repairing the stale env — running `{plan.label}` …")
    os.environ[_SKEW_REPAIR_ATTEMPTED_ENV] = "1"
    result = run_allowed_to_fail(plan.cmd, expected_codes=None)
    if result.returncode != 0:
        typer.echo(f"      Repair FAILED: {result.stderr.strip()[:400]}")
        typer.echo(f"      Manual fix: `{plan.label}`.")
        return False
    remaining = find_version_skew(source / "pyproject.toml")
    if remaining:
        typer.echo(f"      Repair ran but skew persists: {'; '.join(skew.summary for skew in remaining)}")
        return False
    typer.echo("      Repaired — the env now satisfies every declared runtime dependency.")
    return True


def _check_teatree_mcp_liveness() -> bool:
    """EXERCISE teatree's own MCP server — hard FAIL when it is not usable (#4049).

    The counterpart to :func:`_check_teatree_mcp_registration`, which is structural and
    a WARN by design. This one spawns the registered ``t3 mcp serve``, speaks a real
    ``initialize`` frame to it over stdio, and keeps the stderr ``claude mcp list``
    throws away — so an enabled-but-dead server is a FAIL that names its cause and the
    exact remedy instead of one advisory line among twenty.

    Declared-versus-installed skew is checked first and REPAIRED where it is mechanical
    (a stale tool env), because that cause kills the server before it can report
    anything about itself. Only a genuinely unusable server escalates to the operator.

    Degrades to a WARN only when the check cannot RUN at all (no ``t3`` on PATH, an
    unresolvable source tree) — never for a server that ran and failed.
    """
    from teatree.core.mcp_liveness import exercise_mcp_server, skew_finding  # noqa: PLC0415 — keeps CLI startup light
    from teatree.utils.dep_drift import editable_source_path  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.utils.dep_skew import find_version_skew  # noqa: PLC0415 — deferred: keeps CLI startup light

    ok = True
    source = editable_source_path()
    pyproject = source / "pyproject.toml" if source else None
    if pyproject is not None and pyproject.is_file():
        skews = find_version_skew(pyproject)
        if skews:
            typer.echo(f"FAIL  {skew_finding([skew.summary for skew in skews], source=pyproject)}")
            ok = _repair_version_skew(source) if source else False

    argv = _resolve_registered_mcp_command()
    if argv is None:
        typer.echo("WARN  `t3` is not on PATH, so the registered `t3 mcp serve` could not be exercised.")
        return ok

    try:
        outcome = exercise_mcp_server(argv)
    except OSError as exc:
        typer.echo(f"WARN  Could not spawn `{' '.join(argv)}` to exercise the MCP server: {exc}")
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
