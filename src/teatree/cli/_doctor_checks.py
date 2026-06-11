"""Standalone ``_check_*`` helpers invoked by `t3 doctor check`.

Split out of ``teatree.cli.doctor`` (souliane/teatree#1270). Each helper is
narrow (single concern, single typer-echo path) and returns ``bool`` for
pass/fail aggregation. Re-exported from ``teatree.cli.doctor`` for backward
compatibility with existing test imports.
"""

from pathlib import Path

import typer


def _check_single_db() -> bool:
    """Warn if any ``db.sqlite3`` other than the canonical path exists under DATA_DIR."""
    from teatree.paths import CANONICAL_DB, DATA_DIR, find_stale_dbs  # noqa: PLC0415

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
    import teatree  # noqa: PLC0415
    from teatree import paths  # noqa: PLC0415

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


def _check_singletons() -> bool:
    """Clean up stale pid files for known singleton processes."""
    from teatree.utils.singleton import default_pid_path, read_pid  # noqa: PLC0415

    for name in ("teatree-worker", "slack-listener", "loop-tick"):
        path = default_pid_path(name)
        had_file = path.is_file()
        if read_pid(path) is None and had_file:
            typer.echo(f"OK    Cleared stale {name} pid file")
    return True


def _check_editable_sanity() -> bool:
    from teatree.cli.doctor import DoctorService  # noqa: PLC0415

    ok = True
    try:
        for problem in DoctorService.check_editable_sanity():
            typer.echo(f"WARN  {problem}")
            ok = False
    except Exception as exc:  # noqa: BLE001 — overlay loading can fail in many ways
        typer.echo(f"FAIL  Editable sanity check crashed: {exc.__class__.__name__}: {exc}")
        ok = False
    return ok


def _check_skills() -> bool:
    ok = True
    claude_skills = Path.home() / ".claude" / "skills"
    if claude_skills.is_dir():
        from teatree.skill_support.schema import validate_directory  # noqa: PLC0415

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
        from teatree.core.account_switch import detect_and_recover_account_switch  # noqa: PLC0415

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
    import shutil  # noqa: PLC0415

    from teatree.cli.update import _collect_repos  # noqa: PLC0415
    from teatree.utils.venv_artifacts import find_stale_uv_venv  # noqa: PLC0415

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
    ``session_model``, any ``[agent.skill_models]`` floor, or the Fable
    kill-switch ``fable_fallback`` (teatree#2237) is a WARN (it ranks
    most-capable via ``cost.tier_rank``, so it still works, but it is most likely
    a typo). An absent or all-valid config is silently OK.
    """
    from teatree.config_agent import resolve_agent_config  # noqa: PLC0415
    from teatree.core.cost import PRICE_TABLE  # noqa: PLC0415

    try:
        cfg = resolve_agent_config()
    except ValueError as exc:
        typer.echo(f"FAIL  Invalid [agent] session_effort in ~/.teatree.toml: {exc}")
        return False

    def _unrecognised(model: str) -> bool:
        lowered = model.lower()
        return not any(tier in lowered for tier in PRICE_TABLE)

    if cfg.session_model and _unrecognised(cfg.session_model):
        typer.echo(
            f"WARN  [agent] session_model {cfg.session_model!r} matches no known tier "
            f"({', '.join(PRICE_TABLE)}); it will be treated as most-capable. Likely a typo."
        )
    for skill, floor in cfg.skill_models.items():
        if floor and _unrecognised(floor):
            typer.echo(
                f"WARN  [agent.skill_models] {skill} = {floor!r} matches no known tier "
                f"({', '.join(PRICE_TABLE)}); it will be treated as most-capable. Likely a typo."
            )
    if not cfg.fable_enabled and _unrecognised(cfg.fable_fallback):
        typer.echo(
            f"WARN  [agent] fable_fallback {cfg.fable_fallback!r} matches no known tier "
            f"({', '.join(PRICE_TABLE)}); Fable will downgrade to an unknown model. Likely a typo."
        )
    return True


def _check_legacy_overlay_alias() -> None:
    """Warn (never rewrite) on a stale legacy ``[overlays.<alias>]`` table.

    souliane/teatree#1108: older ``slack-bot`` runs wrote a short
    ``[overlays.<alias>]`` table (e.g. ``[overlays.teatree]``) for an
    overlay whose canonical entry-point name is ``t3-<alias>``. Discovery
    now folds such a bare config-only alias table into its canonical
    overlay so it is no longer listed twice — but the stale table is
    confusing to read. Surface it as a WARN with the corrective rename;
    the agent/user does the edit (no auto-rewrite of the user's config).
    """
    try:
        from importlib.metadata import entry_points  # noqa: PLC0415

        from teatree.config import CONFIG_PATH, _match_canonical_ep, load_config  # noqa: PLC0415

        config = load_config(CONFIG_PATH)
        ep_names = {ep.name for ep in entry_points(group="teatree.overlays")}
        for name, overlay_cfg in config.raw.get("overlays", {}).items():
            if name in ep_names or overlay_cfg.get("class") or overlay_cfg.get("path"):
                continue
            canonical = _match_canonical_ep(name, ep_names)
            if canonical is not None:
                typer.echo(
                    f"WARN  Stale '[overlays.{name}]' table in ~/.teatree.toml — "
                    f"the canonical overlay is '{canonical}'. Rename it to "
                    f"'[overlays.{canonical}]' (discovery folds it for now)."
                )
    except Exception:  # noqa: BLE001 — doctor warnings must never crash the run
        return


def _check_dream_staleness() -> bool:
    """Warn when the idle-time dream consolidation cron is stale (#1933).

    The dream pass distils session feedback into the ``ConsolidatedMemory``
    ledger; if it stops succeeding, memories pile up unpromoted unnoticed. The
    alarm keys on the last *successful* run (``DreamRunMarker.is_stale``, 48h):
    a run that keeps failing bumps only the attempt timestamp, so staleness
    keeps firing, and bootstrap (never succeeded) is stale by construction. A
    fresh successful pass clears it; the remedy points at scheduling
    ``t3 dream tick`` (which advances the cadence ledger) rather than a one-off
    ``t3 dream run``. Mirrors the SelfUpdateMarker/MiniLoopMarker-style
    marker-staleness alarms.

    Crash-proof: any error (DB offline, unmigrated self-DB) degrades to OK so a
    doctor run never aborts on this check — same posture as the other
    DB-reading doctor checks.
    """
    from django.utils import timezone  # noqa: PLC0415

    from teatree.core.models import DreamRunMarker  # noqa: PLC0415

    try:
        stale = DreamRunMarker.objects.is_stale(timezone.now())
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Dream-staleness check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not stale:
        return True
    typer.echo(
        "WARN  Dream consolidation is stale — no successful pass in 48h. "
        "Memories pile up unpromoted; schedule `t3 dream tick` (~04:00 cron) so "
        "the cadence ledger advances, not just a one-off `t3 dream run` (#1933).",
    )
    return False
