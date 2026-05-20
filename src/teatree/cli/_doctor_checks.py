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
        from teatree.skill_schema import validate_directory  # noqa: PLC0415

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
