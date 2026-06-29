"""``t3 <overlay> gate`` — the orchestrator's guaranteed self-rescue.

The orchestrator-execution-boundary gate (``handle_enforce_orchestrator_boundary``
in ``hooks/scripts/hook_router.py``, [#115]/[#1472]) can deny the MAIN agent's
heavy foreground ``Bash``. If detection ever misbehaves — e.g. a sub-agent is
misclassified as the main agent — the orchestrator (and, by sidechain
misdetection, every sub-agent) can be locked out of Bash entirely.

This module backs the always-reachable escape hatch. ``t3 <overlay> gate
disable`` flips the durable kill-switch
``[teatree] orchestrator_bash_gate_enabled = false`` in ``~/.teatree.toml``.
The command is unconditionally runnable EVEN WHEN the gate is enabled, because
the gate's heavy-Bash denylist (``_ORCHESTRATOR_HEAVY_BASH_RE``) does not match
a ``t3 …`` command, and ``t3 …`` invocations are the orchestration prefix the
gate is built to allow. The kill-switch lives out-of-repo so it survives
``t3 update``.

A second gate rides the same self-rescue surface: the skill-loading-on-task
gate (``handle_enforce_skill_loading_on_task_create``, [#1488]) can deny a
fanned-out ``TaskCreated`` until the matching teatree skill is loaded. If its
detection ever misbehaves, ``t3 <overlay> gate skill-loading disable`` flips the
``[teatree] skill_loading_gate_enabled = false`` kill-switch — reachable for the
same reason (``t3 …`` is the orchestration prefix every gate allows; the
``TaskCreated`` gate does not govern Bash at all).

This is a pure-Python local read/modify/write of ``~/.teatree.toml`` — it does
NOT route through Django or an overlay ``manage.py`` subprocess, so it stays
runnable even when the heavier overlay machinery is wedged.
"""

import tomllib
from pathlib import Path

import typer

GATE_KEY = "orchestrator_bash_gate_enabled"
SKILL_GATE_KEY = "skill_loading_gate_enabled"
PLAN_GATE_KEY = "plan_edit_gate_enabled"
CONFIG_OVERWRITE_GATE_KEY = "config_overwrite_gate_enabled"
COMPLETION_CLAIM_GATE_KEY = "completion_claim_gate_enabled"
MEMORY_RECALL_GATE_KEY = "memory_recall_enabled"
# Master fail-open switch (NEVER-LOCKOUT). Unlike the per-gate kill-switches
# above (which default ENABLED and read ``is not False``), this is OFF by
# default and reads ``is True`` — it must NEVER relax a gate by accident, only
# by an explicit operator opt-in. When ON, every OVER-DENY gate flips to
# fail-open at once; the PUBLIC-egress leak gate ignores it (fail-closed always).
# The ``danger_`` prefix makes a forgotten override in ``~/.teatree.toml``
# unmissable — this switch disables protective gates wholesale.
DANGER_GATE_FAIL_OPEN_KEY = "danger_gate_fail_open"


def _config_path() -> Path:
    return Path.home() / ".teatree.toml"


def _gate_key_is_enabled(key: str) -> bool:
    """Resolve ``[teatree] <key>`` (default True), failing OPEN to enabled.

    Mirrors the hook layer's gate resolution: the gate is enabled unless an explicit
    ``false`` is recorded. For a cold-hook gate key the resolution is DB-first then TOML —
    matching the flipped hook reader (config-unify PR3) so ``t3 gate status`` reports what
    the gate actually does: a real DB bool wins, otherwise it falls through to the
    ``[teatree]`` TOML value. Fails OPEN to enabled on a missing/broken config + DB so the
    reported status matches the gate's own fail-open posture.
    """
    if _is_cold_hook_gate_key(key):
        from teatree.config import cold_reader  # noqa: PLC0415

        db_value = cold_reader.read_setting(key, scope="")
        if isinstance(db_value, bool):
            return db_value
    config_path = _config_path()
    if not config_path.is_file():
        return True
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return True
    teatree = config.get("teatree") if isinstance(config, dict) else None
    if not isinstance(teatree, dict):
        return True
    return teatree.get(key) is not False


def gate_is_enabled() -> bool:
    """Resolve the orchestrator heavy-Bash gate (``GATE_KEY``, default True)."""
    return _gate_key_is_enabled(GATE_KEY)


def skill_loading_gate_is_enabled() -> bool:
    """Resolve the skill-loading-on-task gate (``SKILL_GATE_KEY``, default True)."""
    return _gate_key_is_enabled(SKILL_GATE_KEY)


def plan_edit_gate_is_enabled() -> bool:
    """Resolve the plan-edit gate (``PLAN_GATE_KEY``, default True)."""
    return _gate_key_is_enabled(PLAN_GATE_KEY)


def config_overwrite_gate_is_enabled() -> bool:
    """Resolve the read-before-overwrite config gate (``CONFIG_OVERWRITE_GATE_KEY``, default True)."""
    return _gate_key_is_enabled(CONFIG_OVERWRITE_GATE_KEY)


def completion_claim_gate_is_enabled() -> bool:
    """Resolve the completion-claim Stop gate (``COMPLETION_CLAIM_GATE_KEY``, default True)."""
    return _gate_key_is_enabled(COMPLETION_CLAIM_GATE_KEY)


def memory_recall_gate_is_enabled() -> bool:
    """Resolve the cold-tier memory recall injector (``MEMORY_RECALL_GATE_KEY``, default True)."""
    return _gate_key_is_enabled(MEMORY_RECALL_GATE_KEY)


def danger_gate_fail_open_is_enabled() -> bool:
    """Resolve the master fail-open switch (``DANGER_GATE_FAIL_OPEN_KEY``, default False).

    Reads ``[teatree] danger_gate_fail_open`` and returns True ONLY when it is
    an explicit ``true``. Fails CLOSED to disabled (the protective default) on a
    missing/broken config or a non-table ``teatree`` section — the inverse
    posture of :func:`gate_is_enabled`, because accidentally relaxing every
    over-deny gate is exactly the failure this switch must never cause. The
    over-deny gates consult this; the PUBLIC-egress leak gate never does.
    """
    config_path = _config_path()
    if not config_path.is_file():
        return False
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    teatree = config.get("teatree") if isinstance(config, dict) else None
    if not isinstance(teatree, dict):
        return False
    return teatree.get(DANGER_GATE_FAIL_OPEN_KEY) is True


def _is_cold_hook_gate_key(key: str) -> bool:
    """Whether *key* is a seeded cold-hook gate (DB tier) vs a TOML-home gate.

    The cold-hook gates (``skill_loading`` / ``plan_edit`` / ``config_overwrite`` /
    ``completion_claim`` / ``memory_recall``) are seeded into the canonical DB by ``t3
    setup`` and read DB-first by the flipped hook reader (config-unify PR3), so ``t3 gate``
    must read/write that SAME DB tier or its toggle is shadowed by the seeded row.
    ``orchestrator_bash_gate_enabled`` and ``danger_gate_fail_open`` are TOML-home (#1775,
    never seeded), so they stay on TOML — the always-available Bash self-rescue. Membership
    is derived from ``COLD_HOOK_SETTINGS`` (inline import — this module is pulled by
    ``teatree.config`` at bootstrap) so it can never drift from the seeded registry.
    """
    from teatree.config import COLD_HOOK_SETTINGS  # noqa: PLC0415

    return key in COLD_HOOK_SETTINGS


def _set_gate_key(key: str, *, enabled: bool) -> Path:
    """Persist ``<key> = <enabled>`` to the tier the gate's reader consults; return that destination.

    For a cold-hook gate key the flipped reader is DB-first (config-unify PR3), so the write
    goes to the canonical DB via the Django-free cold writer — making the toggle authoritative
    over a seeded row, and the returned destination is the canonical DB path. A present-but-
    locked DB (``WRITE_FAILED``) still returns the DB path WITHOUT a TOML fallback: the DB row
    stays authoritative, so the caller's read-back-verify surfaces the locked write rather than
    a dead, shadowed TOML row. Only a genuinely absent DB tier (a fresh, pre-``t3 setup`` cold
    state) or a TOML-home gate key falls through to the ``~/.teatree.toml`` write.
    """
    if _is_cold_hook_gate_key(key):
        from teatree.config import cold_writer  # noqa: PLC0415

        if cold_writer.write_setting(key, enabled) is not cold_writer.WriteResult.NO_DB_TIER:
            return cold_writer.canonical_config_db()

    # ``tomlkit`` is imported inline (matching ``slack_setup``) so loading this
    # module — pulled transitively by ``teatree.config`` on every CLI bootstrap
    # — never eagerly imports the toml-preserving dep.
    import tomlkit  # noqa: PLC0415
    from tomlkit import items as tomlkit_items  # noqa: PLC0415

    config_path = _config_path()
    document = tomlkit.parse(config_path.read_text(encoding="utf-8")) if config_path.is_file() else tomlkit.document()
    teatree = document.get("teatree")
    if not isinstance(teatree, tomlkit_items.Table):
        teatree = tomlkit.table()
        document["teatree"] = teatree
    teatree[key] = enabled
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")
    return config_path


def _write_gate_and_verify(key: str, *, enabled: bool) -> Path:
    """Write ``<key>=<enabled>``, verify the toggle actually took, and return the real destination.

    After the write, read the gate back through :func:`_gate_key_is_enabled` — DB-first for a
    cold-hook key, exactly as the flipped hook reader resolves it. If the observed state
    disagrees with *enabled*, the canonical DB was locked (or the write otherwise failed) and
    the toggle did NOT take: raise ``typer.Exit(1)`` with a loud message so the command never
    prints a success line over a stale, still-effective gate. The returned destination lets the
    caller report where the value ACTUALLY landed (the canonical DB vs the ``~/.teatree.toml``
    fallback). Catching the mismatch by read-back rather than by classifying the write error
    covers EVERY failure mode, regardless of cause.
    """
    destination = _set_gate_key(key, enabled=enabled)
    if _gate_key_is_enabled(key) != enabled:
        still = "ENABLED" if not enabled else "DISABLED"
        typer.echo(
            f"ERROR: `{key}` did NOT take — the canonical DB is locked or the write failed; "
            f"the gate is still {still}. Retry once the DB is free.",
            err=True,
        )
        raise typer.Exit(code=1)
    return destination


def _register_keyed_gate(parent: typer.Typer, *, name: str, key: str, label: str) -> None:
    """Attach a ``status``/``disable``/``enable`` subgroup for ``[teatree] <key>``."""
    group = typer.Typer(no_args_is_help=True, help=f"{label} kill-switch (self-rescue).")

    @group.command(name="status")
    def status() -> None:
        """Show whether the gate is enabled."""
        typer.echo("gate ENABLED" if _gate_key_is_enabled(key) else "gate DISABLED — no-op")

    @group.command(name="disable")
    def disable() -> None:
        """Disable the gate (self-rescue from a lockout)."""
        destination = _write_gate_and_verify(key, enabled=False)
        typer.echo(f"gate DISABLED — wrote `{key} = false` to {destination}")

    @group.command(name="enable")
    def enable() -> None:
        """Re-enable the gate."""
        destination = _write_gate_and_verify(key, enabled=True)
        typer.echo(f"gate ENABLED — wrote `{key} = true` to {destination}")

    parent.add_typer(group, name=name)


def register_gate_commands(overlay_app: typer.Typer) -> None:
    """Attach the ``gate`` subgroup (heavy-Bash + skill-loading kill-switches)."""
    gate_group = typer.Typer(
        no_args_is_help=True,
        help="Enforcement-gate kill-switches (self-rescue).",
    )

    @gate_group.command(name="status")
    def status() -> None:
        """Show whether the orchestrator heavy-Bash gate is enabled."""
        if gate_is_enabled():
            typer.echo("gate ENABLED — heavy orchestrator bash blocked")
        else:
            typer.echo("gate DISABLED — no-op")

    @gate_group.command(name="disable")
    def disable() -> None:
        """Disable the gate (self-rescue from a Bash lockout)."""
        destination = _write_gate_and_verify(GATE_KEY, enabled=False)
        typer.echo(f"gate DISABLED — wrote `{GATE_KEY} = false` to {destination}")

    @gate_group.command(name="enable")
    def enable() -> None:
        """Re-enable the gate."""
        destination = _write_gate_and_verify(GATE_KEY, enabled=True)
        typer.echo(f"gate ENABLED — wrote `{GATE_KEY} = true` to {destination}")

    _register_keyed_gate(
        gate_group,
        name="skill-loading",
        key=SKILL_GATE_KEY,
        label="Skill-loading-on-task gate",
    )

    _register_keyed_gate(
        gate_group,
        name="plan",
        key=PLAN_GATE_KEY,
        label="Plan-before-code edit-block gate",
    )

    _register_keyed_gate(
        gate_group,
        name="config-overwrite",
        key=CONFIG_OVERWRITE_GATE_KEY,
        label="Read-before-overwrite config/dotfile gate",
    )

    _register_keyed_gate(
        gate_group,
        name="completion-claim",
        key=COMPLETION_CLAIM_GATE_KEY,
        label="Completion-claim gate (on-target evidence before done)",
    )

    _register_keyed_gate(
        gate_group,
        name="memory-recall",
        key=MEMORY_RECALL_GATE_KEY,
        label="Cold-tier memory recall injector",
    )

    overlay_app.add_typer(gate_group, name="gate")


def register_fail_open_gate_commands(review_app: typer.Typer) -> None:
    """Attach ``review gate fail-open enable|disable|status`` to the review app.

    The master fail-open switch lives under ``t3 review gate fail-open`` (the
    same surface as the rest of the review-gate machinery). ``enable`` flips
    every OVER-DENY gate to fail-open at once; ``disable`` restores their
    protective posture; ``status`` reports the current state. Default OFF.
    """
    gate_group = typer.Typer(no_args_is_help=True, help="Review-gate master switches.")
    fail_open = typer.Typer(no_args_is_help=True, help="Master fail-open switch for the over-deny gates.")

    @fail_open.command(name="status")
    def status() -> None:
        """Show whether the master fail-open switch is on."""
        if danger_gate_fail_open_is_enabled():
            typer.echo("fail-open ON — every over-deny gate is fail-open (leak gate still fail-closed)")
        else:
            typer.echo("fail-open OFF — over-deny gates enforce normally")

    @fail_open.command(name="enable")
    def enable() -> None:
        """Turn the master fail-open switch ON (self-rescue from an over-deny lockout)."""
        destination = _write_gate_and_verify(DANGER_GATE_FAIL_OPEN_KEY, enabled=True)
        typer.echo(f"fail-open ON — wrote `{DANGER_GATE_FAIL_OPEN_KEY} = true` to {destination}")

    @fail_open.command(name="disable")
    def disable() -> None:
        """Turn the master fail-open switch OFF (restore normal gate enforcement)."""
        destination = _write_gate_and_verify(DANGER_GATE_FAIL_OPEN_KEY, enabled=False)
        typer.echo(f"fail-open OFF — wrote `{DANGER_GATE_FAIL_OPEN_KEY} = false` to {destination}")

    gate_group.add_typer(fail_open, name="fail-open")
    review_app.add_typer(gate_group, name="gate")
