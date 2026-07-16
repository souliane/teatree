"""Standalone ``_check_*`` helpers invoked by `t3 doctor check`.

Split out of ``teatree.cli.doctor`` (souliane/teatree#1270). Each helper is
narrow (single concern, single typer-echo path) and returns ``bool`` for
pass/fail aggregation. Re-exported from ``teatree.cli.doctor`` for backward
compatibility with existing test imports.
"""

import contextlib
from pathlib import Path

import typer

from teatree.loop.preset_resolution import consistency_findings


def _check_single_db() -> bool:
    """Warn if any ``db.sqlite3`` other than the canonical path exists under DATA_DIR."""
    from teatree.paths import CANONICAL_DB, DATA_DIR, find_stale_dbs  # noqa: PLC0415 — deferred: lazy CLI import

    stale = list(find_stale_dbs(DATA_DIR, canonical=CANONICAL_DB))
    if not stale:
        return True
    for path in stale:
        typer.echo(f"WARN  Stale DB at {path} — canonical DB is {CANONICAL_DB}. Remove to silence.")
    return False


def _check_entrypoint_is_primary_clone() -> bool:
    """FAIL when the running ``t3`` entrypoint is anchored to a worktree (#1507).

    The installed long-lived ``t3`` must import ``teatree`` from the primary
    clone. A stale editable ``.pth`` anchored to a worktree makes the resident
    code resolve a per-worktree isolated DB (``paths.DATA_DIR_AUTO_ISOLATED``
    is then ``True``) while the loop and canonical state live in the true
    canonical DB — work silently vanishes. This is a hard FAIL, not a WARN,
    naming the offending worktree, both DB paths, and the remediation.

    Reads the live :mod:`teatree.paths` attributes (resolved at that module's
    import time from the entrypoint's on-disk location), so it reports the
    state of the process actually running ``t3 doctor``.
    """
    import teatree  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree import paths  # noqa: PLC0415 — deferred: keeps CLI startup light

    if not paths.DATA_DIR_AUTO_ISOLATED:
        return True
    # ``teatree.__file__`` is ``<repo>/src/teatree/__init__.py``; the repo root
    # is its third parent (matches ``paths._code_repo_root``).
    repo_root = Path(teatree.__file__).resolve().parents[2]
    isolated_db = paths.DATA_DIR / "db.sqlite3"
    typer.echo(
        f"FAIL  Entrypoint is anchored to a worktree, not the primary clone: {repo_root}. "
        f"The installed t3 resolves the isolated DB {isolated_db} instead of the canonical "
        f"DB {paths.TRUE_CANONICAL_DB} — loop state and merges silently diverge. Re-anchor "
        f"the editable install at the primary clone: re-run `t3 setup` from the primary "
        f"clone (or fix the stale `.pth`), then re-run `t3 doctor check`.",
    )
    return False


def _check_dangling_editable_pth() -> bool:
    """FAIL when the teatree editable ``.pth`` or uv receipt points at a gone dir.

    The reaped-worktree footgun: a sub-agent repointed the GLOBAL uv-tool
    ``teatree.pth`` at its own worktree, which ``clean-all`` later reaped, leaving
    the ``.pth`` dangling so every ``t3`` died with ``ModuleNotFoundError: No
    module named 't3_bootstrap'`` machine-wide. This detects that dangling state
    (and the sibling uv-receipt ``editable`` clone) before it can wedge the next
    invocation, and auto-repairs the ``.pth`` to ``$T3_REPO/src`` when it is SAFE
    to do so — i.e. only when the running ``t3`` is already importing teatree from
    the canonical clone (never from a worktree, which would re-anchor the global
    install at a transient checkout, the #1507 hazard).

    A healthy install passes silently. Crash-proof: any unexpected error degrades
    to a pass so this diagnostic never aborts the whole doctor run.
    """
    from teatree.utils.editable_pth import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        canonical_src_dir,
        detect_dangling_editable,
        repair_pth_to_canonical,
        running_from_canonical_clone,
    )

    try:
        dangling = detect_dangling_editable()
    except Exception as exc:  # noqa: BLE001 — an inspection failure warns and passes, never blocks doctor
        typer.echo(f"WARN  Could not inspect the teatree editable .pth: {exc}")
        return True
    if not dangling.is_dangling:
        return True

    canonical = canonical_src_dir()
    pth = dangling.pth
    if (
        pth is not None
        and dangling.pth_dangling_dir is not None
        and canonical is not None
        and running_from_canonical_clone()
        and repair_pth_to_canonical(pth, canonical)
    ):
        typer.echo(
            f"WARN  Repaired dangling teatree editable .pth (was {dangling.pth_dangling_dir}, "
            f"now {canonical}). The reaped worktree it pointed at would have broken t3 machine-wide."
        )
        # Re-evaluate after the repair so a stale, pre-repair snapshot does not
        # FAIL on (and tell the user to re-anchor) a .pth this run just healed.
        # Any genuinely-unrelated receipt problem is preserved by the re-detect.
        dangling = detect_dangling_editable()
        if not dangling.is_dangling:
            return True

    pth_still_dangling = dangling.pth_dangling_dir is not None
    if pth_still_dangling:
        typer.echo(
            f"FAIL  teatree editable .pth points at a non-existent dir: {dangling.pth_dangling_dir} "
            f"({dangling.pth}). A reaped worktree left it dangling — t3 dies with "
            f"ModuleNotFoundError. Re-anchor: re-run `t3 setup` from the canonical clone "
            f"(or rewrite the .pth to $T3_REPO/src), then `cd $T3_REPO && uv tool install --editable . --force`."
        )
    if dangling.receipt_source is not None:
        typer.echo(
            f"FAIL  uv tool receipt records a non-existent editable source: {dangling.receipt_source}. "
            f"It re-breaks the .pth on the next `t3 update`/reinstall. Fix: "
            f"`cd $T3_REPO && uv tool install --editable . --force`."
        )
    return False


def _check_t3_shim_receipt(*, repair: bool = False) -> bool:
    """FAIL when the active ``t3`` shim serves an editable install from the wrong checkout (#3231).

    A second, unrelated ``uv tool install --editable <other-checkout>`` under the
    same ``teatree`` package/entrypoint name silently steals the global ``t3``
    shim — and a moved/renamed checkout re-points the receipt at a stale path.
    Either way the receipt's ``requirements[].editable`` no longer matches the
    expected checkout (``$T3_REPO``), yet ``t3`` keeps resolving against the
    wrong source until a command fails deep inside. Unlike the dangling-``.pth``
    check (which fires only when the target is GONE), this catches a target that
    EXISTS but is the wrong clone.

    Only meaningful with a known expected checkout: when ``$T3_REPO`` is unset,
    or the install is not an editable uv-tool install (no receipt editable
    source), the check skips (returns ``True``). On a mismatch it FAILs with the
    remediation; with ``repair=True`` it re-points the install via
    ``uv tool install --editable <checkout> --force`` and passes. Crash-proof:
    any inspection failure degrades to a pass so it never aborts the doctor run.
    """
    from teatree.utils.editable_pth import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        expected_checkout,
        receipt_editable_source,
        repair_receipt_to_checkout,
    )

    try:
        expected = expected_checkout()
        source = receipt_editable_source()
    except Exception as exc:  # noqa: BLE001 — an inspection failure warns and passes, never blocks doctor
        typer.echo(f"WARN  Could not inspect the t3 shim's uv receipt: {exc}")
        return True
    if expected is None or source is None or source.resolve() == expected:
        return True

    if repair and repair_receipt_to_checkout(expected):
        typer.echo(f"WARN  Re-pointed the t3 editable install at {expected} (uv receipt recorded {source}).")
        return True
    typer.echo(
        f"FAIL  The active t3 shim's uv receipt records an editable source {source} that does not "
        f"match the expected checkout {expected} — a relocated or same-name-hijacked editable install "
        f"is serving t3 from the wrong path. Re-point it: `t3 doctor check --repair` "
        f"(or `uv tool install --editable {expected} --force`)."
    )
    return False


def _check_singletons() -> bool:
    """Clean up stale pid files for known singleton processes."""
    from teatree.utils.singleton import (  # noqa: PLC0415 (deferred: keeps the doctor-check import light)
        WORKER_SINGLETON,
        default_pid_path,
        read_pid,
    )

    for name in (WORKER_SINGLETON, "slack-listener", "loop-tick"):
        path = default_pid_path(name)
        had_file = path.is_file()
        if read_pid(path) is None and had_file:
            typer.echo(f"OK    Cleared stale {name} pid file")
    return True


def _check_worker_running() -> bool:
    """WARN when the loop worker is enabled but not running (PR-28).

    Default-ON ``loop_runner_enabled`` with a FREE ``worker`` flock means no worker is
    draining the loop-timer chains — the loops are silently dead. Actionable: run
    ``t3 worker ensure``. Read-only; always returns ``True`` (a WARN, not a hard FAIL),
    and any read error is swallowed so the doctor run never crashes on it.
    """
    # A doctor check must never crash the doctor run — any read error is swallowed.
    with contextlib.suppress(Exception):
        from teatree.config import get_effective_settings  # noqa: PLC0415 (deferred: light doctor-check import)
        from teatree.utils.singleton import WORKER_SINGLETON, flock_is_held  # noqa: PLC0415 (deferred: light import)

        if get_effective_settings().loop_runner_enabled and not flock_is_held(WORKER_SINGLETON):
            typer.echo("WARN  loop_runner_enabled is ON but no worker holds the flock — run `t3 worker ensure`")
    return True


def _check_editable_sanity() -> bool:
    from teatree.cli.doctor import DoctorService  # noqa: PLC0415 — deferred: breaks checks ↔ doctor cycle

    ok = True
    try:
        for problem in DoctorService.check_editable_sanity():
            typer.echo(f"WARN  {problem}")
            ok = False
    except Exception as exc:  # noqa: BLE001 — overlay loading can fail in many ways
        typer.echo(f"FAIL  Editable sanity check crashed: {exc.__class__.__name__}: {exc}")
        ok = False
    return ok


def _check_ttyd_for_dashboard(env: dict[str, str] | None = None) -> bool:
    """WARN when the admin box serves the dashboard but ``ttyd`` is missing (#3263).

    The dashboard's loopback "Debug session" button spawns a ``ttyd`` terminal
    (``teatree.agents.terminal_launcher.launch_ttyd``, resolved via
    ``shutil.which("ttyd")``). Only the ``admin`` role serves the dashboard, so
    the check flags a missing ``ttyd`` solely when ``TEATREE_ROLE == "admin"`` —
    a worker/init box (or a plain host that never opens the dashboard) is not
    affected. Surfacing-only: always returns ``True`` so it never gates the
    doctor exit code.
    """
    import os  # noqa: PLC0415 — deferred: loaded only when this command runs
    import shutil  # noqa: PLC0415 — deferred: loaded only when this command runs

    resolved_env = env if env is not None else dict(os.environ)
    if resolved_env.get("TEATREE_ROLE") != "admin":
        return True
    if shutil.which("ttyd") is not None:
        return True
    typer.echo(
        "WARN  ttyd is not installed but this box serves the admin dashboard — the "
        "'Debug session' loopback terminal will fail. Install it (`apt install ttyd`)."
    )
    return True


_CHROME_DEVTOOLS_MCP_NAME = "chrome-devtools"


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
    typer.echo(
        "INFO  chrome-devtools MCP is an OPTIONAL aid for authoring/debugging Playwright "
        "e2e specs (live DOM, console, network, screenshots). Enable it with "
        "`claude mcp add chrome-devtools -- npx -y chrome-devtools-mcp@latest` (needs a "
        "Chrome executable). It is never required — its absence gates nothing."
    )
    return True


def _check_skills() -> bool:
    ok = True
    claude_skills = Path.home() / ".claude" / "skills"
    if claude_skills.is_dir():
        from teatree.skill_support.schema import validate_directory  # noqa: PLC0415 — deferred: keeps CLI startup light

        errors, warnings = validate_directory(claude_skills)
        for warning in warnings:
            typer.echo(f"WARN  {warning}")
        for error in errors:
            typer.echo(f"FAIL  {error}")
            ok = False
        if not errors:
            skill_count = sum(1 for d in claude_skills.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())
            typer.echo(f"OK    {skill_count} skill(s) validated")
    return ok


def _check_account_switch() -> bool:
    """Detect a mid-session ``/login`` switch and report connector recovery (#1916).

    Runs the detect-invalidate-reprobe cycle. A clean run (no switch, or a
    switch where every connector re-probed reachable) is OK. A switch that
    leaves a connector unreachable is a hard FAIL — the stale bridge would
    otherwise route DMs silently to the old workspace. Crash-proof: any error
    degrades to a WARN so a doctor run never aborts on this check.
    """
    try:
        from teatree.core.account_switch import detect_and_recover_account_switch  # noqa: PLC0415 — lazy CLI import

        outcome = detect_and_recover_account_switch()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Account-switch check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not outcome.switched:
        return True
    if outcome.all_reachable:
        typer.echo(
            f"OK    Claude account switch recovered ({outcome.previous_fingerprint[:8]}… → "
            f"{outcome.current_fingerprint[:8]}…); backend cache reinvalidated, connectors reachable.",
        )
        return True
    unreachable = ", ".join(f"{p.name} ({p.detail})" for p in outcome.probes if not p.reachable)
    typer.echo(
        f"FAIL  Claude account switch detected ({outcome.previous_fingerprint[:8]}… → "
        f"{outcome.current_fingerprint[:8]}…) but connectors are unreachable: {unreachable}. "
        "Re-auth the MCP connector(s) in the Claude.ai UI, then re-run `t3 doctor check`.",
    )
    return False


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
            "connected in `claude mcp list` — it may not have started for this session yet.",
        )
    return True


def _check_stale_uv_venv() -> bool:
    """Detect + clean an empty uv-built ``.venv`` in a Pipfile-managed clone (#2005).

    A clone carrying a ``Pipfile`` that also holds an in-project ``.venv`` built
    by uv with nothing installed is a wrong-toolchain artifact — it shadows
    pipenv's managed venvs and poisons both ``uv run`` and ``pipenv run``. Walks
    every repo the other repo-scoped doctor gates audit (:func:`_collect_repos`),
    removes each offending ``.venv``, and WARNs. Removal makes the next run a
    no-op (idempotent). Crash-proof: any error degrades to a WARN so the doctor
    run never aborts on this check.
    """
    import shutil  # noqa: PLC0415 — deferred: loaded only when this command runs

    from teatree.cli.update import _collect_repos  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.utils.venv_artifacts import find_stale_uv_venv  # noqa: PLC0415 — deferred: keeps CLI startup light

    ok = True
    for _name, repo in _collect_repos():
        try:
            stale = find_stale_uv_venv(repo)
            if stale is None:
                continue
            shutil.rmtree(stale)
            typer.echo(
                f"WARN  Removed empty uv-built .venv shadowing pipenv in {repo} ({stale.name}). "
                "It poisoned both `uv run` and `pipenv run`; pipenv will rebuild its own venv."
            )
            ok = False
        except OSError as exc:
            typer.echo(
                f"WARN  Could not remove empty uv-built .venv in {repo}: {exc}. "
                "Delete it manually (`rm -rf .venv`), then re-run `t3 doctor check`."
            )
            ok = False
    return ok


def _check_agent_session_pins() -> bool:
    """Validate the ``[agent]`` model + effort settings (teatree#2216).

    A ``session_effort`` off the strict CLI scale (``low|medium|high|xhigh|max``)
    is a hard FAIL — ``resolve_agent_config`` raises, and we surface the message
    rather than letting it reach the interactive spawn. An unrecognised model in
    ``session_model`` or any ``[agent.skill_models]`` floor is a WARN (it ranks
    most-capable via ``cost.tier_rank``, so it still works, but it is most likely
    a typo). An absent or all-valid config is silently OK.

    The recognition is model-vocabulary-aware (F4): a bare pin passes when it is
    an abstract tier (``frontier``), a shipped tier-model id, a Claude family
    (``opus`` short-name or a dated id), or the operator's OWN ``agent_tier_models``
    value; a provider-prefixed id (anything carrying a ``/`` — ``deepseek/…``,
    ``orcarouter/…``) is a deliberate non-Claude pin and always passes. Only a
    bare token that is NONE of these (a genuine typo) warns.
    """
    from teatree.agents.model_tiering import known_model_vocabulary  # noqa: PLC0415 — deferred: keep import light
    from teatree.config.agent_spawn import resolve_agent_config  # noqa: PLC0415 — deferred: keep import light
    from teatree.core.cost import FAMILY_TO_TIER  # noqa: PLC0415 — deferred: keep import light

    try:
        cfg = resolve_agent_config()
    except ValueError as exc:
        typer.echo(f"FAIL  Invalid agent_session_effort setting: {exc}")
        return False

    known = known_model_vocabulary() | {value.lower() for value in cfg.tier_models.values()}

    def _unrecognised(model: str) -> bool:
        lowered = model.lower()
        if "/" in lowered:  # a deliberate provider-native pin (deepseek/…, orcarouter/…)
            return False
        if any(family in lowered for family in FAMILY_TO_TIER):  # a Claude family short-name or dated id
            return False
        return lowered not in known

    if cfg.session_model and _unrecognised(cfg.session_model):
        typer.echo(
            f"WARN  [agent] session_model {cfg.session_model!r} matches no known tier or model id; "
            "it will be treated as most-capable. Likely a typo."
        )
    for skill, floor in cfg.skill_models.items():
        if floor and _unrecognised(floor):
            typer.echo(
                f"WARN  [agent.skill_models] {skill} = {floor!r} matches no known tier or model id; "
                "it will be treated as most-capable. Likely a typo."
            )
    return True


def _check_legacy_overlay_alias() -> None:
    """Warn (never rewrite) on a stale legacy alias entry in the DB overlays registry.

    souliane/teatree#1108: older ``slack-bot`` runs recorded a short overlay entry
    (e.g. ``teatree``) for an overlay whose canonical entry-point name is
    ``t3-<alias>``. Discovery now folds such a bare config-only alias entry into
    its canonical overlay so it is no longer listed twice — but the stale entry is
    confusing to read. Surface it as a WARN with the corrective rename; the
    agent/user does the edit (no auto-rewrite of the user's registry).
    """
    try:
        from importlib.metadata import entry_points  # noqa: PLC0415 — deferred: loaded only when this command runs

        from teatree.config import _match_canonical_ep, load_config  # noqa: PLC0415 — deferred: keeps CLI startup light

        config = load_config()
        ep_names = {ep.name for ep in entry_points(group="teatree.overlays")}
        for name, overlay_cfg in config.raw.get("overlays", {}).items():
            if name in ep_names or overlay_cfg.get("class") or overlay_cfg.get("path"):
                continue
            canonical = _match_canonical_ep(name, ep_names)
            if canonical is not None:
                typer.echo(
                    f"WARN  Stale overlay entry '{name}' in the DB overlays registry — "
                    f"the canonical overlay is '{canonical}'. Rename it to "
                    f"'{canonical}' (discovery folds it for now)."
                )
    except Exception:  # noqa: BLE001 — doctor warnings must never crash the run
        return


def _check_stale_path_t3(env: dict[str, str] | None = None) -> bool:
    import os  # noqa: PLC0415 — deferred: loaded only when this command runs

    resolved_env = env if env is not None else dict(os.environ)
    path_dirs = [Path(d) for d in resolved_env.get("PATH", "").split(os.pathsep) if d]
    home = Path(resolved_env.get("HOME", str(Path.home())))
    uv_tool_bin_dir_str = resolved_env.get("UV_TOOL_BIN_DIR")
    uv_bin_dir = Path(uv_tool_bin_dir_str) if uv_tool_bin_dir_str else home / ".local" / "bin"
    uv_bin_dir_resolved = uv_bin_dir.resolve()

    uv_pos = next(
        (i for i, d in enumerate(path_dirs) if d.resolve() == uv_bin_dir_resolved and (d / "t3").is_file()),
        None,
    )
    if uv_pos is None:
        return True

    shadows = [d / "t3" for i, d in enumerate(path_dirs) if i < uv_pos and (d / "t3").is_file()]
    if not shadows:
        return True

    uv_t3 = uv_bin_dir / "t3"
    for shadow in shadows:
        typer.echo(
            f"FAIL  Shadowing t3 at {shadow} precedes the uv-managed {uv_t3} on PATH. "
            f"This stale entry masks dep updates. Remove it: rm {shadow}",
        )
    return False


def _check_slack_socket_mode() -> bool:
    """Report + auto-fix Slack Socket Mode readiness per overlay (#106 / BLUEPRINT § B5).

    Extends the existing Slack scope auto-management to Socket Mode: for every
    Slack-backed overlay it validates the app-level ``xapp-`` token (present,
    prefixed, carrying ``connections:write``) and auto-fixes the manifest's
    socket-mode flag / events / bot scopes via ``apps.manifest.update`` where the
    app-config token allows. Slack has no API to mint an app-level token, so an
    absent one is surfaced as a single ACTION with its exact URL + ``pass`` slot.

    Surfacing-only: always returns ``True`` so it never gates the overall doctor
    exit code (Slack is optional — it must never become mandatory). Crash-proof:
    any error degrades to a WARN so a doctor run never aborts on this check.
    """
    try:
        from teatree.cli.slack.socket_doctor import check_slack_socket_mode  # noqa: PLC0415 — only when probe runs

        outcome = check_slack_socket_mode()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Slack Socket Mode check crashed: {exc.__class__.__name__}: {exc}")
        return True
    for finding in outcome.findings:
        typer.echo(f"{finding.level.value:<5} [{finding.overlay}] {finding.message}")
    return True


def _check_loop_presets() -> bool:
    """Warn on a dangling loop-preset reference (#3159): deleted preset / loop / schedule.

    Presets, slots and the active-schedule selector reference loops and presets BY
    NAME, so a deleted target fails open to base config at read time — but the
    dangling reference should still be surfaced. Reports each such finding (never
    repairs). Crash-proof: any error degrades to OK so a doctor run never aborts,
    same posture as the other DB-reading checks.
    """
    try:
        findings = consistency_findings()
    except Exception as exc:  # noqa: BLE001  # doctor check must never crash the run
        typer.echo(f"WARN  Loop-preset consistency check crashed: {exc.__class__.__name__}: {exc}")
        return False
    if not findings:
        return True
    for finding in findings:
        typer.echo(f"WARN  Loop preset: {finding}")
    return False


def _check_dream_staleness() -> bool:
    """Warn when the idle-time dream consolidation cron is stale (#1933).

    The dream pass distils session feedback into the ``ConsolidatedMemory``
    ledger; if it stops succeeding, memories pile up unpromoted unnoticed. The
    alarm keys on the last *successful* run (``DreamRunMarker.is_stale``, 48h):
    a run that keeps failing bumps only the attempt timestamp, so staleness
    keeps firing, and bootstrap (never succeeded) is stale by construction. A
    fresh successful pass clears it; the remedy points at scheduling
    ``t3 dream tick`` (which advances the cadence ledger) rather than a one-off
    ``t3 dream run``. Mirrors the SelfUpdateMarker-style marker-staleness alarms.

    Crash-proof: any error (DB offline, unmigrated self-DB) degrades to OK so a
    doctor run never aborts on this check — same posture as the other
    DB-reading doctor checks.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.core.models import DreamRunMarker  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        stale = DreamRunMarker.objects.is_stale(timezone.now())
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Dream-staleness check crashed: {exc.__class__.__name__}: {exc}")
        return False
    if not stale:
        return True
    typer.echo(
        "WARN  Dream consolidation is stale — no successful pass in 48h. "
        "Memories pile up unpromoted; schedule `t3 dream tick` (~04:00 cron) so "
        "the cadence ledger advances, not just a one-off `t3 dream run` (#1933).",
    )
    return False
