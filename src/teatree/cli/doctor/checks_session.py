"""``_check_*`` probes for Slack / session identity invoked by `t3 doctor check`.

Each helper is narrow (single concern, single ``typer.echo`` path) and returns
``bool`` for pass/fail aggregation by :func:`teatree.cli.doctor.app.run_doctor_checks`.
"""

import typer


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
