"""Defer a Typer sub-app's module import until its subcommand is actually reached.

A Typer sub-app has to exist as an object when the parent calls ``add_typer``, so
registering one imports its whole module tree at CLI startup — every invocation pays
for every subcommand. ``t3 eval`` is the expensive case: its module chain reaches
``claude_agent_sdk`` and then ``mcp``, and MEASURED on the fork's own venv that is
7.7-15s of the 23.4s ``import teatree.cli``, paid by commands with no relation to
evals (``t3 teatree config_setting get`` among them) and paid TWICE for an overlay
command, because ``teatree.cli.overlay.managepy_core`` spawns a second interpreter.

The placeholder registered here carries only the group's own metadata - its name and
help text - which is all the parent's ``--help`` listing reads, so `t3 --help`
renders identically without importing anything. Everything that needs more than the
metadata loads the real app first:

*   ``make_context`` is the invocation seam. Click resolves a subcommand, then calls
    ``make_context`` on it and invokes ``sub_ctx.command`` - so delegating this one
    method hands the REAL group the parsing and the running, and the real callback,
    its options and ``invoke_without_command`` all apply unchanged. ``t3 eval``'s
    bare form runs the whole suite through such a callback, so a placeholder that
    kept its own would have silently become a no-op.
*   ``params`` is the group's OWN options - ``t3 eval``'s bare form takes six, and
    the CLI-reference generator reads them off the command (``command_tree`` line
    160). A placeholder answering "no options" would publish a reference documenting
    a command that accepts nothing.
*   ``commands`` is the enumeration seam. ``TyperGroup.list_commands``, click's
    ``get_command`` and the typo suggester all read it, and so do the command
    catalogue and CLI-reference walkers (``teatree.cli.command_tree`` recurses with
    ``list_commands``/``get_command``). Routing it at the attribute keeps every one of
    them COMPLETE rather than quietly short: an under-reported catalogue would make
    the ``skill-command-validity`` lane pass on commands it never saw.

So laziness is invisible except in time: the only calls it skips are the ones that
never needed the module. The cost returns in full for `t3 eval --help`, which loads
the real tree to print it.
"""

from typing import TYPE_CHECKING, Any

from typer.core import TyperGroup

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping

    import click
    import typer

__all__ = ["lazy_typer_group"]


def lazy_typer_group(loader: "Callable[[], typer.Typer]") -> type[TyperGroup]:
    """A ``TyperGroup`` subclass that builds *loader*'s app on first real use.

    Returned as a class because Typer instantiates the group itself
    (``get_group_from_info`` reads ``Typer(cls=...)``), so the loader cannot be
    handed over per instance and is bound here instead.
    """

    class _LazyGroup(TyperGroup):
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 — pass-through to click's own untyped `Group.__init__`
            # Both before `super().__init__`: click's initialiser ASSIGNS `commands`,
            # which lands on the setter below and would otherwise read an attribute
            # that does not exist yet.
            self._lazy_real: TyperGroup | None = None
            self._lazy_placeholder: MutableMapping[str, click.Command] = {}
            self._lazy_placeholder_params: list[click.Parameter] = []
            super().__init__(*args, **kwargs)

        def _real(self) -> TyperGroup:
            from typer.main import get_group  # noqa: PLC0415 — deferred: only on the load path

            if self._lazy_real is None:
                self._lazy_real = get_group(loader())
            return self._lazy_real

        @property
        def commands(self) -> "MutableMapping[str, click.Command]":
            return self._real().commands

        @property
        def params(self) -> "list[click.Parameter]":
            return self._real().params

        @params.setter
        def params(self, value: "list[click.Parameter]") -> None:
            self._lazy_placeholder_params = value

        @commands.setter
        def commands(self, value: "MutableMapping[str, click.Command]") -> None:
            # The placeholder's own (empty) mapping. Kept rather than dropped so the
            # attribute round-trips, but never consulted - the real group owns the
            # answer, and answering from here is exactly the silent under-report the
            # module docstring rules out.
            self._lazy_placeholder = value

        def make_context(
            self,
            info_name: "str | None",
            args: list[str],
            parent: "click.Context | None" = None,
            **extra: Any,  # noqa: ANN401 — mirrors click's `make_context(**extra: Any)` contract
        ) -> "click.Context":
            return self._real().make_context(info_name, args, parent=parent, **extra)

    return _LazyGroup
