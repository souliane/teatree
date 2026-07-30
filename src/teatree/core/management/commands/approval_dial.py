"""``t3 <overlay> approval_dial`` — the per-action-class approval dial operator surface (#119).

Flipping an action class between ``ask`` (the ship default) and ``auto`` (graduated
autonomy) is a DB-home ``ConfigSetting`` write: one ``approval_dial`` row per scope
holding a ``{action_class: "ask"|"auto"}`` JSON dict, which
:mod:`teatree.core.models.approval_dial` reads at ask-time. This command is the
documented way to do that flip — ``set``/``clear`` mutate the table, ``show`` renders
the resolved dial and each class's effective verdict (never-fades floor, configured
trust, and metric re-tighten folded in).

A never-fades class (``public_issue_create`` / ``gate_or_policy_change``) is refused on
``auto`` — the dial floors it to ASK regardless, so storing ``auto`` would only mislead.

Non-zero exits use ``raise SystemExit(N)`` — this runs under Django's ``call_command``.
"""

from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.models import ConfigSetting
from teatree.core.models.approval_dial import DIAL_CONFIG_KEY, NEVER_FADES, configured_trust, effective_decision
from teatree.core.models.approval_metrics import compute_metrics
from teatree.core.models.approval_policy import ACTION_CLASSES
from teatree.core.models.trust_level import TrustLevel

_OverlayOption = Annotated[
    str,
    typer.Option("--overlay", help="Overlay scope for the row; omit for the global scope (every overlay)."),
]


def _scope_label(scope: str) -> str:
    return "global" if not scope else f"overlay {scope!r}"


def _load_table(scope: str) -> dict[str, str]:
    """The stored ``{class: trust}`` table for exactly *scope* (not layered), or ``{}``."""
    stored = ConfigSetting.objects.get_effective(DIAL_CONFIG_KEY, scope=scope)
    return {str(k): str(v) for k, v in stored.items()} if isinstance(stored, dict) else {}


class Command(TyperCommand):
    @command()
    def set(
        self,
        action_class: Annotated[str, typer.Argument(help="Action class to flip.")],
        trust: Annotated[str, typer.Argument(help="Trust level: ask or auto.")],
        overlay: _OverlayOption = "",
    ) -> None:
        """Set *action_class*'s trust to *trust* in *overlay*'s dial table (merging).

        Refuses an unknown class, an invalid trust word, and ``auto`` on a never-fades
        class (which the dial floors to ASK regardless).
        """
        if action_class not in ACTION_CLASSES:
            self.stderr.write(f"  refusing: {action_class!r} is not an approval action class")
            raise SystemExit(2)
        try:
            level = TrustLevel(trust.strip().lower())
        except ValueError:
            self.stderr.write(f"  refusing: {trust!r} is not a trust level (ask|auto)")
            raise SystemExit(2) from None
        if level is TrustLevel.AUTO and action_class in NEVER_FADES:
            self.stderr.write(
                f"  refusing: {action_class!r} is a never-fades class — the dial floors it to ASK regardless"
            )
            raise SystemExit(2)
        table = _load_table(overlay)
        table[action_class] = level.value
        ConfigSetting.objects.set_value(DIAL_CONFIG_KEY, table, scope=overlay)
        self.stdout.write(f"  set {action_class} = {level.value}  [{_scope_label(overlay)}]")

    @command()
    def clear(
        self,
        action_class: Annotated[str, typer.Argument(help="Action class to remove from the dial table.")],
        overlay: _OverlayOption = "",
    ) -> None:
        """Remove *action_class* from *overlay*'s dial table (it falls back to ASK).

        Deletes the whole ``approval_dial`` row once its last class is removed. Exits
        non-zero when the class is not set in that scope, so a typo is loud.
        """
        table = _load_table(overlay)
        if action_class not in table:
            self.stderr.write(f"  no dial entry for {action_class}  [{_scope_label(overlay)}]")
            raise SystemExit(1)
        del table[action_class]
        if table:
            ConfigSetting.objects.set_value(DIAL_CONFIG_KEY, table, scope=overlay)
        else:
            ConfigSetting.objects.clear(DIAL_CONFIG_KEY, scope=overlay)
        self.stdout.write(f"  cleared {action_class}  [{_scope_label(overlay)}]")

    @command()
    def show(self, overlay: _OverlayOption = "") -> None:
        """Render every class's configured trust, never-fades floor, breach, and verdict.

        Reads the RESOLVED table (global then *overlay* on top) so it shows what the dial
        actually decides for that scope right now, not just the raw stored row.
        """
        scope_arg = overlay or None
        self.stdout.write(f"  approval dial  [{_scope_label(overlay)}]:")
        for action_class in sorted(ACTION_CLASSES):
            trust = configured_trust(action_class, overlay=scope_arg).value
            verdict = effective_decision(action_class, overlay=scope_arg).value
            metrics = compute_metrics(action_class)
            never = " never-fades" if action_class in NEVER_FADES else ""
            breach = " BREACHED" if metrics.breached else ""
            self.stdout.write(
                f"    {action_class:<22} trust={trust:<4} → {verdict}{never}{breach}"
                f"  (interventions={metrics.interventions}, declines={metrics.declines},"
                f" defect_escapes={metrics.defect_escapes}, rework={metrics.rework})"
            )
