"""``t3 <overlay> autonomy`` — show / set the per-overlay trust switch.

The ``autonomy`` switch (``babysit`` < ``notify`` < ``full``, default
``babysit``) is the single per-overlay knob that collapses the three
user-in-the-loop approval gates — ``on_behalf_post_mode`` (which gates
colleague auto-approve / on-behalf posts), ``require_human_approval_to_merge``,
``require_human_approval_to_answer`` — and pins ``mode = auto`` so the
loop's auto-merge path is reachable. It also drives review-request blocking off
the tier (#2579): the ``notify`` tier resolves ``review_request_post_disabled =
True`` (a collaborative/customer surface never auto-requests review), while
``full`` resolves it ``False`` (a solo tooling surface auto-requests). The
collapse and its precedence rules live in
:func:`teatree.config._apply_autonomy`; this command is the first-class CLI
surface that persists the knob so a user flips an overlay to full merge/approve
autonomy without hand-editing config.

``autonomy`` is DB-home (#1775): its sole authoritative tier is the
``ConfigSetting`` store, so ``set`` writes a DB row — the active overlay's
OVERLAY-scoped row by default, ``--overlay <name>`` to target a specific one,
and ``--global`` the GLOBAL-scope (workspace-wide) row instead. A value in
``[overlays.<name>]`` / ``[teatree]`` TOML is ignored on read, so writing the
store is what makes the value take effect. ``show`` reports the effective value
for the active overlay (env / overlay-row / global-row / default resolved via
:func:`teatree.config.get_effective_settings`).

The knob NEVER touches the always-on safety/quality floor: independent
cold-review (maker != checker), CI-green-before-merge, the privacy/leak
gate, the never-lockout posture, and the substrate recorded-approver
keystone all stay in force under every tier. It only decides *whether to
ask the user*, never *whether the work is correct*.

The ``autonomy`` value is DB-home, so ``set`` writes a ``ConfigSetting`` row.
But the typer overlay app is assembled in the ``t3`` console-script process,
which has NOT run ``django.setup()`` — so touching the ``ConfigSetting`` ORM
in-process crashes with ``ImproperlyConfigured`` the moment the model layer
imports (souliane/teatree#2622). ``set`` therefore delegates the write to the
``config_setting`` management command via :func:`teatree.cli.overlay.managepy_core`
— the same ``python -m teatree`` subprocess seam every other DB-touching overlay
command (``followup``, ``safe_kill``, …) uses, where ``django.setup()`` runs
once per process. ``show`` is a pure resolver read whose DB tier fails safe to
the default, so it needs no bootstrap.
"""

import json

import typer

from teatree.config import Autonomy

AUTONOMY_KEY = "autonomy"


def _active_overlay_name() -> str | None:
    from teatree.config import _active_overlay_entry  # noqa: PLC0415 — deferred: keeps CLI startup light

    entry = _active_overlay_entry()
    return entry.name if entry is not None else None


def _write_setting_row(value: str, *, scope: str = "") -> None:
    """Persist the ``autonomy`` row via the ``config_setting`` management command.

    Delegates to the ``python -m teatree config_setting set`` subprocess seam
    (:func:`teatree.cli.overlay.managepy_core`) so the ORM write runs in a process
    where ``django.setup()`` has been called — never in the unbootstrapped
    console-script process that assembled the typer app (#2622). The management
    command parses its ``value`` as JSON and validates it through the same
    registry parser the resolver uses on read, so the value is JSON-encoded here.
    An empty ``scope`` addresses the GLOBAL store; a name scopes the row to that
    overlay.
    """
    from teatree.cli.overlay import managepy_core  # noqa: PLC0415 — deferred: breaks autonomy ↔ overlay cycle

    args = ["config_setting", "set", AUTONOMY_KEY, json.dumps(value)]
    if scope:
        args += ["--overlay", scope]
    managepy_core(*args, overlay_name=scope)


def _set_global_autonomy(level: Autonomy) -> str:
    """Persist the workspace-wide GLOBAL-scope ``autonomy`` row; return its store label."""
    _write_setting_row(level.value)
    return "the global config store"


def _set_overlay_autonomy(level: Autonomy, *, overlay: str) -> str:
    """Persist ``autonomy`` as *overlay*'s OVERLAY-scoped ``ConfigSetting`` row; return its label.

    The overlay-scoped store row is the setting's authoritative tier, so a value
    written here is what ``get_effective_settings(overlay)`` reads for it.
    """
    _write_setting_row(level.value, scope=overlay)
    return f"overlay {overlay!r}'s config store"


def register_autonomy_commands(overlay_app: typer.Typer) -> None:
    """Attach the ``autonomy`` subgroup (``show`` / ``set``) to an overlay app."""
    autonomy_group = typer.Typer(no_args_is_help=True, help="Per-overlay trust switch (collapses the approval gates).")

    @autonomy_group.command(name="show")
    def show() -> None:
        """Show the effective autonomy tier (DB overlay-scope > DB global-scope > default; no env layer)."""
        from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps CLI startup light
        from teatree.utils.django_bootstrap import ensure_django  # noqa: PLC0415 — deferred: keeps CLI startup light

        # ``get_effective_settings`` reads the ``ConfigSetting`` DB tier via the
        # app registry, which fails SAFE to ``{}`` when Django is not configured.
        # In the ``t3`` console-script process ``django.setup()`` has NOT run, so
        # without this bootstrap the DB tier is silently skipped and ``show``
        # reports the dataclass DEFAULT instead of the persisted tier (#2622). It
        # is idempotent, so a test/loop process that already configured Django is
        # unaffected.
        ensure_django()
        typer.echo(get_effective_settings().autonomy.value)

    @autonomy_group.command(name="set")
    def set_(
        level: str = typer.Argument(help="babysit | notify | full"),
        *,
        overlay: str = typer.Option(
            "",
            "--overlay",
            help="Overlay name to scope the value to (default: the active overlay). Ignored with --global.",
        ),
        write_global: bool = typer.Option(
            False,
            "--global",
            help="Write the workspace-wide [teatree] default instead of a per-overlay value.",
        ),
    ) -> None:
        """Persist the autonomy knob. A typo is rejected; the safety floor is never relaxed."""
        try:
            parsed = Autonomy.parse(level)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        if write_global:
            location = _set_global_autonomy(parsed)
        else:
            target_overlay = overlay or _active_overlay_name()
            if not target_overlay:
                typer.echo(
                    "No active overlay to scope `autonomy` to; pass --overlay <name> or --global.",
                    err=True,
                )
                raise typer.Exit(code=1)
            location = _set_overlay_autonomy(parsed, overlay=target_overlay)
        typer.echo(f"autonomy = {parsed.value} — wrote the {AUTONOMY_KEY} row to {location}")

    overlay_app.add_typer(autonomy_group, name="autonomy")
