"""One ``t3 <overlay> <group>`` command group: its subcommands and where each one dispatches."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DjangoGroup:
    """One overlay sub-app group description.

    ``core_dispatch`` flags groups whose subcommands live in
    ``teatree.core.management.commands`` (not in any overlay-owned
    ``manage.py``). When ``True``, :meth:`OverlayAppBuilder._bridge_subcommand`
    dispatches via :func:`managepy_core` (``python -m teatree``) so the call
    never reaches an overlay's ``manage.py`` whose settings module may not
    register the command (#1318 follow-up to #1312).

    ``core_subcommands`` is the *per-subcommand* variant for a mixed group:
    most subcommands route through the overlay ``manage.py`` (they drive the
    overlay's own behaviour) but a named few must run in the teatree-core
    runtime. ``db approve`` is the canonical case — it records a ``DbApproval``
    row in the teatree-core control DB the gate reads, so it must run in the
    runtime process (``python -m teatree``) rather than route through an overlay
    ``manage.py`` whose self-DB lacks the row (#953/#126).

    ``overlay_settings_subcommands`` is the third variant: subcommands that must
    run in the active overlay's **own** Django settings context when it ships
    one, so an overlay-shipped Django app (and its migrations) is in
    ``INSTALLED_APPS``. ``db migrate`` is the case — it must apply BOTH the
    teatree-core migrations AND the overlay app's migrations against the same
    canonical control DB. The core ``teatree.settings`` cannot see the overlay
    app: its entry-point app discovery imports the overlay class at
    settings-bootstrap time, which raises before the app registry is ready (or
    on a missing overlay dependency), so the overlay app is dropped and its
    migrations are structurally invisible on the ``python -m teatree`` path. The
    overlay's own settings module lists the app explicitly and resolves the same
    canonical DB, so :meth:`OverlayAppBuilder._bridge_subcommand` routes migrate
    through the overlay ``manage.py`` when the overlay ships its own settings
    module, and keeps the in-process core path when it runs on the base
    ``teatree.settings`` (nothing extra to load) or ships no project dir.
    """

    help_text: str
    subcommands: list[tuple[str, str]]
    core_dispatch: bool = False
    core_subcommands: frozenset[str] = frozenset()
    overlay_settings_subcommands: frozenset[str] = frozenset()

    def dispatches_to_core(self, sub_name: str) -> bool:
        """True iff *sub_name* must dispatch via teatree-core, not overlay manage.py."""
        return self.core_dispatch or sub_name in self.core_subcommands

    def needs_overlay_settings(self, sub_name: str) -> bool:
        """True iff *sub_name* must run in the overlay's own settings context when it ships one."""
        return sub_name in self.overlay_settings_subcommands

    def resolve_core_dispatch(self, sub_name: str, *, ships_own_overlay_settings: bool) -> bool:
        """Whether *sub_name* dispatches via teatree-core, given this overlay's settings.

        Core-dispatched when the group/subcommand is marked core-only, OR when it
        is an overlay-settings subcommand (``db migrate``) but the overlay does
        NOT ship its own settings module — the overlay ``manage.py`` context
        would add nothing (its ``INSTALLED_APPS`` are the core ones), so the
        in-process core path is kept (#126, no ``uv --directory`` hop). An
        overlay-settings subcommand on an overlay that DOES ship its own settings
        module routes to the overlay ``manage.py``, where the overlay app (and
        its migrations) is in ``INSTALLED_APPS``.
        """
        if self.dispatches_to_core(sub_name):
            return True
        return self.needs_overlay_settings(sub_name) and not ships_own_overlay_settings
