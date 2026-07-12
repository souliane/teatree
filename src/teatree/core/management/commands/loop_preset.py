"""``manage.py loop_preset`` — list/show/use/auto/create/edit/delete loop presets (#3159).

Backs ``t3 loop preset …``. A preset is a named, owner-editable, DB-stored
loop-state mask; ``use`` activates one as a manual override (L3), ``auto`` clears
it so the schedule (L2) decides again. Presets never rewrite ``Loop``/``LoopState``
rows — activation is read-time only. ORM access lives in a management command (the
project's "anything touching the ORM is a management command" rule).
"""

import datetime as dt
import json
import re
from typing import Annotated, Any, NoReturn

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command

from teatree.core.models import PIN_MODES, Loop, LoopPreset, LoopPresetOverride
from teatree.loop.preset_resolution import next_boundary
from teatree.loops.preset_status import active_summary, effective_verdicts

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_ENTRY_VALUES = {"on": True, "off": False}


def _parse_duration(raw: str) -> dt.timedelta:
    match = _DURATION_RE.match(raw.strip())
    if match is None:
        msg = f"invalid --for duration {raw!r}; use forms like 2h, 30m, 1d"
        raise ValueError(msg)
    return dt.timedelta(seconds=int(match.group(1)) * _DURATION_UNIT_SECONDS[match.group(2)])


def _apply_entry_edits(entries: object, edits: list[str]) -> dict[str, bool]:
    """Fold ``inbox=on`` / ``review=off`` / ``dream=inherit`` edits into *entries* (a copy).

    *entries* is the raw stored map (a JSONField value, so ``object``); non-bool
    existing values (a corrupt / legacy row) are dropped, so an edit always produces
    a clean tri-state map.
    """
    updated: dict[str, bool] = (
        {str(key): value for key, value in entries.items() if isinstance(value, bool)}
        if isinstance(entries, dict)
        else {}
    )
    for edit in edits:
        loop_name, _, value = edit.partition("=")
        loop_name = loop_name.strip()
        value = value.strip().lower()
        if not loop_name or (value not in _ENTRY_VALUES and value != "inherit"):
            msg = f"invalid --set {edit!r}; use <loop>=on|off|inherit"
            raise ValueError(msg)
        if value == "inherit":
            updated.pop(loop_name, None)
        else:
            updated[loop_name] = _ENTRY_VALUES[value]
    return updated


def _unknown_entry_loops(entries: dict[str, bool]) -> list[str]:
    """Entry keys that name no existing ``Loop`` — a warn-not-refuse validation hint."""
    known = set(Loop.objects.values_list("name", flat=True))
    return sorted(name for name in entries if name not in known)


class Command(TyperCommand):
    help = "List/show/use/auto/create/edit/delete loop presets (#3159)."

    @command(name="list")
    def list_presets(self, *, json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False) -> None:
        """List every preset with its pin, scope, entry count, and the ACTIVE marker."""
        active = active_summary()
        active_name = active.name if active is not None else ""
        presets = list(LoopPreset.objects.all())
        if json_output:
            payload = [_preset_row(preset, active_name) for preset in presets]
            self.stdout.write(json.dumps({"active": active_name, "presets": payload}, indent=2))
            return
        if not presets:
            self.stdout.write("No presets defined. Run `t3 setup` to seed the defaults.")
            return
        self.stdout.write("presets:")
        for preset in presets:
            marker = " *ACTIVE*" if preset.name == active_name else ""
            pin = f" pin={preset.availability_pin}" if preset.availability_pin else ""
            scope = f" scope={','.join(preset.overlay_scope_names)}" if preset.overlay_scope_names else ""
            self.stdout.write(f"  {preset.name:<16} {preset.entry_count} entries{pin}{scope}{marker}")
            if preset.description:
                self.stdout.write(f"      {preset.description}")

    @command(name="show")
    def show(
        self,
        name: Annotated[str, typer.Argument(help="Preset to show; omit for the active preset + WHY.")] = "",
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Show a named preset, or (no arg) the active preset + WHY + per-loop verdict table."""
        if name:
            self._show_named(name, json_output=json_output)
        else:
            self._show_active(json_output=json_output)

    @command(name="use")
    def use(
        self,
        name: Annotated[str, typer.Argument(help="Preset to activate as a manual override.")],
        *,
        for_: Annotated[str, typer.Option("--for", help="TTL like 2h/30m/1d (default: until the next boundary).")] = "",
        until: Annotated[str, typer.Option("--until", help="Explicit ISO-8601 expiry instant.")] = "",
        hold: Annotated[bool, typer.Option("--hold", help="Sticky: hold until explicitly cleared.")] = False,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Activate *name* as the L3 manual override (default: until the next scheduled boundary)."""
        if LoopPreset.objects.by_name(name) is None:
            self._refuse(f"no preset named {name!r} — run `t3 loop preset list`", json_output=json_output)
        until_dt = self._resolve_until(for_=for_, until=until, hold=hold, json_output=json_output)
        LoopPresetOverride.objects.set_override(name, until=until_dt)
        window = "held until cleared" if until_dt is None else f"until {until_dt.isoformat()}"
        self._emit(
            {"preset": name, "until": until_dt.isoformat() if until_dt else None},
            f"loop preset {name!r} active ({window}).",
            json_output=json_output,
        )

    @command(name="auto")
    def auto(self, *, json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False) -> None:
        """Clear the manual override so the active schedule decides again."""
        cleared = LoopPresetOverride.objects.clear()
        message = (
            "cleared the manual override — the schedule decides again." if cleared else "no manual override was set."
        )
        self._emit({"cleared": cleared}, message, json_output=json_output)

    @command(name="create")
    def create(
        self,
        name: Annotated[str, typer.Argument(help="New preset name (slug).")],
        *,
        set_: Annotated[list[str], typer.Option("--set", help="Entry edit <loop>=on|off (repeatable).")] = [],  # noqa: B006 — typer Option default — idiomatic mutable default for a repeatable flag
        description: Annotated[str, typer.Option("--description", help="Human description.")] = "",
        pin: Annotated[str, typer.Option("--pin", help="Availability pin: present|away|autonomous_away.")] = "",
        scope: Annotated[str, typer.Option("--scope", help="Comma-separated overlay allowlist.")] = "",
    ) -> None:
        """Create a new preset from ``--set`` entries, optional pin and overlay scope."""
        if LoopPreset.objects.by_name(name) is not None:
            self._refuse(f"preset {name!r} already exists — use `edit`", json_output=False)
        preset = LoopPreset.objects.create(
            name=name,
            entries=self._entries_from_edits({}, set_, json_output=False),
            description=description,
            availability_mode=self._validated_pin(pin, json_output=False),
            overlay_scope=_scope_list(scope),
        )
        self._emit_preset_saved(preset, json_output=False)

    @command(name="edit")
    def edit(
        self,
        name: Annotated[str, typer.Argument(help="Preset to edit.")],
        *,
        set_: Annotated[list[str], typer.Option("--set", help="Entry edit <loop>=on|off|inherit (repeatable).")] = [],  # noqa: B006 — typer Option default — idiomatic mutable default for a repeatable flag
        description: Annotated[str, typer.Option("--description", help="Replace the description.")] = "",
        pin: Annotated[str, typer.Option("--pin", help="Replace the availability pin (empty string clears).")] = "",
        scope: Annotated[str, typer.Option("--scope", help="Replace the overlay allowlist.")] = "",
    ) -> None:
        """Edit a preset's entries / description / pin / scope in place."""
        preset = LoopPreset.objects.by_name(name)
        if preset is None:
            self._refuse(f"no preset named {name!r}", json_output=False)
        preset.entries = self._entries_from_edits(preset.entries, set_, json_output=False)
        if description:
            preset.description = description
        if pin:
            preset.availability_mode = self._validated_pin(pin, json_output=False)
        if scope:
            preset.overlay_scope = _scope_list(scope)
        preset.save()
        self._emit_preset_saved(preset, json_output=False)

    @command(name="delete")
    def delete(
        self,
        name: Annotated[str, typer.Argument(help="Preset to delete.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Delete a preset (a slot/override still pointing at it fails open to base config)."""
        deleted, _ = LoopPreset.objects.filter(name=name).delete()
        if not deleted:
            self._refuse(f"no preset named {name!r}", json_output=json_output)
        self._emit({"deleted": name}, f"deleted preset {name!r}.", json_output=json_output)

    def _show_named(self, name: str, *, json_output: bool) -> None:
        preset = LoopPreset.objects.by_name(name)
        if preset is None:
            self._refuse(f"no preset named {name!r}", json_output=json_output)
        unknown = _unknown_entry_loops(preset.entries)
        if json_output:
            self.stdout.write(json.dumps(_preset_detail(preset, unknown), indent=2))
            return
        self.stdout.write(f"preset {preset.name}: {preset.description}")
        for loop_name, value in sorted(preset.entries.items()):
            self.stdout.write(f"  {loop_name:<22} {'on' if value else 'off'}")
        if unknown:
            self.stdout.write(f"  WARN entries name unknown loops: {', '.join(unknown)}")

    def _show_active(self, *, json_output: bool) -> None:
        now = timezone.now()
        summary = active_summary(now)
        verdicts = effective_verdicts(now)
        if json_output:
            self.stdout.write(
                json.dumps(
                    {
                        "active": _summary_payload(summary),
                        "loops": [
                            {"name": v.name, "admitted": v.admitted, "layer": v.layer, "detail": v.detail}
                            for v in verdicts
                        ],
                    },
                    indent=2,
                )
            )
            return
        if summary is None:
            self.stdout.write("active preset: none (no override, no active schedule) — loops run per base config.")
        else:
            pin = f", pins availability {summary.availability_pin}" if summary.availability_pin else ""
            until = "" if summary.until is None else f", until {summary.until.isoformat()}"
            self.stdout.write(f"active preset: {summary.name}  (why: {summary.reason}{until}{pin})")
        self.stdout.write("per-loop effective verdict:")
        for verdict in verdicts:
            state = "run" if verdict.admitted else "masked"
            self.stdout.write(f"  {verdict.name:<22} {state:<7} [{verdict.layer}] {verdict.detail}")

    def _resolve_until(self, *, for_: str, until: str, hold: bool, json_output: bool) -> dt.datetime | None:
        if hold:
            return None
        if for_:
            try:
                return timezone.now() + _parse_duration(for_)
            except ValueError as exc:
                self._refuse(str(exc), json_output=json_output)
        if until:
            parsed = _parse_iso(until)
            if parsed is None:
                self._refuse(f"invalid --until {until!r}; use ISO-8601", json_output=json_output)
            return parsed
        return next_boundary()

    def _entries_from_edits(self, entries: object, edits: list[str], *, json_output: bool) -> dict[str, bool]:
        try:
            return _apply_entry_edits(entries, edits)
        except ValueError as exc:
            self._refuse(str(exc), json_output=json_output)

    def _validated_pin(self, pin: str, *, json_output: bool) -> str:
        value = pin.strip()
        if value and value not in PIN_MODES:
            self._refuse(f"invalid --pin {pin!r}; use present|away|autonomous_away", json_output=json_output)
        return value

    def _emit_preset_saved(self, preset: LoopPreset, *, json_output: bool) -> None:
        unknown = _unknown_entry_loops(preset.entries)
        message = f"saved preset {preset.name!r} ({preset.entry_count} entries)."
        if unknown:
            message += f" WARN entries name unknown loops: {', '.join(unknown)}"
        self._emit(_preset_detail(preset, unknown), message, json_output=json_output)

    def _emit(self, payload: dict[str, Any], message: str, *, json_output: bool) -> None:
        self.stdout.write(json.dumps(payload, indent=2) if json_output else message)

    def _refuse(self, message: str, *, json_output: bool) -> NoReturn:
        self.stdout.write(json.dumps({"error": message}, indent=2) if json_output else f"ERROR  {message}")
        raise SystemExit(2)


def _preset_row(preset: LoopPreset, active_name: str) -> dict[str, Any]:
    return {
        "name": preset.name,
        "description": preset.description,
        "pin": preset.availability_pin,
        "scope": preset.overlay_scope_names,
        "entry_count": preset.entry_count,
        "active": preset.name == active_name,
    }


def _preset_detail(preset: LoopPreset, unknown: list[str]) -> dict[str, Any]:
    return {
        "name": preset.name,
        "description": preset.description,
        "entries": preset.entries,
        "availability_mode": preset.availability_mode,
        "overlay_scope": preset.overlay_scope_names,
        "unknown_loops": unknown,
    }


def _summary_payload(summary: object) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "name": summary.name,  # ty: ignore[unresolved-attribute]
        "layer": summary.layer,  # ty: ignore[unresolved-attribute]
        "reason": summary.reason,  # ty: ignore[unresolved-attribute]
        "until": summary.until.isoformat() if summary.until else None,  # ty: ignore[unresolved-attribute]
        "availability_pin": summary.availability_pin,  # ty: ignore[unresolved-attribute]
    }


def _scope_list(scope: str) -> list[str]:
    return [part.strip() for part in scope.split(",") if part.strip()]


def _parse_iso(raw: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)
