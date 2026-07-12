"""``t3 <overlay> gate`` — the orchestrator's guaranteed self-rescue.

The orchestrator-execution-boundary gate (``handle_enforce_orchestrator_boundary``
in ``hooks/scripts/hook_router.py``, [#115]/[#1472]) can deny the MAIN agent's
heavy foreground ``Bash``. If detection ever misbehaves — e.g. a sub-agent is
misclassified as the main agent — the orchestrator (and, by sidechain
misdetection, every sub-agent) can be locked out of Bash entirely.

This module backs the always-reachable escape hatch. ``t3 <overlay> gate
disable`` flips the durable ``orchestrator_bash_gate_enabled`` kill-switch — DB-home
(every setting lives in the canonical config DB), so it writes and reads that DB
via the Django-free cold writer/reader. The cold writer needs no Django, so the
self-rescue still works when the heavier overlay machinery is wedged.
The command is unconditionally runnable EVEN WHEN the gate is enabled, because
the gate's heavy-Bash denylist (``_ORCHESTRATOR_HEAVY_BASH_RE``) does not match
a ``t3 …`` command, and ``t3 …`` invocations are the orchestration prefix the
gate is built to allow.

A second gate rides the same self-rescue surface: the skill-loading-on-task
gate (``handle_enforce_skill_loading_on_task_create``, [#1488]) can deny a
fanned-out ``TaskCreated`` until the matching teatree skill is loaded. If its
detection ever misbehaves, ``t3 <overlay> gate skill-loading disable`` flips the
``skill_loading_gate_enabled`` kill-switch — reachable for the same reason
(``t3 …`` is the orchestration prefix every gate allows; the ``TaskCreated``
gate does not govern Bash at all).

Every read/write is a Django-free stdlib access of the canonical config DB — it
does NOT route through Django or an overlay ``manage.py`` subprocess, so it stays
runnable even when the heavier overlay machinery is wedged.
"""

from pathlib import Path

import typer

GATE_KEY = "orchestrator_bash_gate_enabled"
SKILL_GATE_KEY = "skill_loading_gate_enabled"
PLAN_GATE_KEY = "plan_edit_gate_enabled"
CONFIG_OVERWRITE_GATE_KEY = "config_overwrite_gate_enabled"
COMPLETION_CLAIM_GATE_KEY = "completion_claim_gate_enabled"
MAIN_CLONE_GATE_KEY = "main_clone_guard_gate_enabled"
MEMORY_RECALL_GATE_KEY = "memory_recall_enabled"
SNAPSHOT_BASELINE_GATE_KEY = "snapshot_baseline_gate_enabled"
GATE_RELAXATION_GATE_KEY = "gate_relaxation_gate_enabled"
OUT_OF_BAND_MERGE_GATE_KEY = "out_of_band_merge_gate_enabled"
STANDING_GOAL_GATE_KEY = "standing_goal_stop_gate_enabled"
# Master fail-open switch (NEVER-LOCKOUT). Unlike the per-gate kill-switches
# above (which default ENABLED and read ``is not False``), this is OFF by
# default and reads ``is True`` — it must NEVER relax a gate by accident, only
# by an explicit operator opt-in. When ON, every OVER-DENY gate flips to
# fail-open at once; the PUBLIC-egress leak gate ignores it (fail-closed always).
# The ``danger_`` prefix makes a forgotten ``true`` unmissable — this switch
# disables protective gates wholesale.
DANGER_GATE_FAIL_OPEN_KEY = "danger_gate_fail_open"


def _gate_key_is_enabled(key: str) -> bool:
    """Resolve the DB-home ``<key>`` gate (default True), failing OPEN to enabled.

    The gate is enabled unless an explicit ``false`` is recorded in the canonical
    config DB. Reads via the Django-free cold reader, so ``t3 teatree gate status``
    reports what the flipped hook reader sees. Fails OPEN to enabled on a
    missing/broken DB so the reported status matches the gate's own fail-open posture.
    """
    from teatree.config import cold_reader  # noqa: PLC0415 — deferred: keeps CLI startup light

    return cold_reader.bool_setting(key, default=True)


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

    Reads the DB-home ``danger_gate_fail_open`` setting and returns True ONLY when
    it is an explicit ``true``. Fails CLOSED to disabled (the protective default) on
    a missing/broken DB — the inverse posture of :func:`gate_is_enabled`, because
    accidentally relaxing every over-deny gate is exactly the failure this switch
    must never cause. The over-deny gates consult this; the PUBLIC-egress leak gate
    never does.
    """
    from teatree.config import cold_reader  # noqa: PLC0415 — deferred: keeps CLI startup light

    return cold_reader.bool_setting(DANGER_GATE_FAIL_OPEN_KEY, default=False)


def _set_gate_key(key: str, *, enabled: bool) -> Path:
    """Persist ``<key> = <enabled>`` to the canonical config DB; return the DB path.

    Every gate key is DB-home, so the write goes to the canonical DB via the
    Django-free cold writer and the returned destination is the canonical DB path.
    A missing DB tier or a locked write is caught by the caller's read-back-verify —
    the toggle does not silently land somewhere the reader ignores.
    """
    from teatree.config import cold_writer  # noqa: PLC0415 — deferred: keeps CLI startup light

    cold_writer.write_setting(key, enabled)
    return cold_writer.canonical_config_db()


def _write_gate_and_verify(key: str, *, enabled: bool) -> Path:
    """Write ``<key>=<enabled>``, verify the toggle actually took, and return the DB destination.

    After the write, read the gate back through :func:`_gate_key_is_enabled` (the same
    canonical-DB read the flipped hook reader resolves). If the observed state disagrees
    with *enabled*, the canonical DB was missing or locked (or the write otherwise
    failed) and the toggle did NOT take: raise ``typer.Exit(1)`` with a loud message so
    the command never prints a success line over a stale, still-effective gate. Catching
    the mismatch by read-back rather than by classifying the write error covers EVERY
    failure mode, regardless of cause.
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
        name="main-clone",
        key=MAIN_CLONE_GATE_KEY,
        label="Main-clone working-tree mutation gate",
    )

    _register_keyed_gate(
        gate_group,
        name="memory-recall",
        key=MEMORY_RECALL_GATE_KEY,
        label="Cold-tier memory recall injector",
    )

    _register_keyed_gate(
        gate_group,
        name="snapshot-baseline",
        key=SNAPSHOT_BASELINE_GATE_KEY,
        label="Snapshot-baseline attestation gate",
    )

    _register_keyed_gate(
        gate_group,
        name="gate-relaxation",
        key=GATE_RELAXATION_GATE_KEY,
        label="Anti-relaxation + tach-soundness gate",
    )

    _register_keyed_gate(
        gate_group,
        name="raw-merge",
        key=OUT_OF_BAND_MERGE_GATE_KEY,
        label="Out-of-band raw-merge gate",
    )

    _register_keyed_gate(
        gate_group,
        name="standing-goal",
        key=STANDING_GOAL_GATE_KEY,
        label="Standing verified-green stop-gate",
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
