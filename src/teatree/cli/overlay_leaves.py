"""Overlay CLI leaves that forward their args verbatim to a ``teatree.core`` command.

``safe-kill`` / ``do`` / ``signals`` are thin overlay shortcuts over same-named
core management commands. Each accepts arbitrary trailing args and forwards them
unparsed — the leaf never redeclares the command's own options. Split out of
``overlay.py`` as its own concern (the near-identical registration boilerplate was
triplicated inline there).
"""

import typer

# A passthrough leaf takes arbitrary trailing args and never parses them itself.
_CORE_PASSTHROUGH_CONTEXT: dict[str, bool] = {
    "allow_extra_args": True,
    "allow_interspersed_args": False,
    "ignore_unknown_options": True,
}

# leaf name -> one-line help. A leaf name's hyphens map to the core command's
# underscores (``safe-kill`` -> ``safe_kill``).
_CORE_PASSTHROUGH_LEAVES: tuple[tuple[str, str], ...] = (
    ("safe-kill", "Signal a pid only if it maps to a dead target AND is confirmed non-live (#2225)."),
    ("do", "Walk a ticket through the lifecycle via each phase's existing gate (PR-31)."),
    ("signals", "Read-only factory quality/velocity signals over the trailing window (SIG-PR-1)."),
)


def register_core_passthrough_leaves(overlay_app: typer.Typer, overlay_name: str) -> None:
    """Register every core-passthrough leaf on *overlay_app*.

    Each leaf dispatches to its same-named ``teatree.core`` management command via
    ``python -m teatree`` (:func:`teatree.cli.overlay.managepy_core`), never the
    overlay clone's own ``manage.py`` — a cwd inside a ticket worktree must not
    resolve that worktree's ``manage.py`` (#1318). ``T3_OVERLAY_NAME`` is set on
    the subprocess env so a scoped command (``signals``) reads this overlay; the
    faithful-child-exit bridge propagates the child exit code.
    """
    for name, help_text in _CORE_PASSTHROUGH_LEAVES:
        _register_leaf(overlay_app, overlay_name, name=name, help_text=help_text)


def _register_leaf(overlay_app: typer.Typer, overlay_name: str, *, name: str, help_text: str) -> None:
    @overlay_app.command(
        name=name,
        help=help_text,
        context_settings=_CORE_PASSTHROUGH_CONTEXT,
        add_help_option=False,
    )
    def _leaf(ctx: typer.Context) -> None:
        from teatree.cli.overlay import managepy_core  # noqa: PLC0415 — deferred to avoid a load-time cycle

        managepy_core(name.replace("-", "_"), *ctx.args, overlay_name=overlay_name)
