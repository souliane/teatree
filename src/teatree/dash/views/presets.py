"""The schedule + preset editor POSTs — the normal handle on what the fleet runs (#3559).

Every mutation here delegates to the ``teatree.loops`` write seams
(``preset_editing`` / ``preset_admin`` / ``schedule_editing``) — the same ones
``t3 loop preset use`` / ``preset edit`` / ``schedule set-active`` call, so
the two-plane enable/disable invariant and the tri-state preset semantics hold
whichever surface the operator uses. The views coordinate only: parse the POST,
call the seam, audit, redirect.
"""

from typing import TYPE_CHECKING

from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from teatree.dash import audit
from teatree.dash.preset_editor import build_preset_editor
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import actor, nav_context
from teatree.loops.preset_admin import create_preset, delete_preset, rename_preset, update_preset_meta
from teatree.loops.preset_editing import PresetEditError, activate_preset, clear_preset_override, set_preset_entry
from teatree.loops.schedule_editing import (
    clear_active_schedule,
    delete_schedule_slot,
    set_active_schedule,
    upsert_schedule_slot,
)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


@require_loopback_or_staff
@require_GET
def presets(request: "HttpRequest") -> "HttpResponse":
    """The editor page — a tab per preset, plus the schedule slot editor."""
    view = build_preset_editor(selected=request.GET.get("preset", "").strip())
    return render(request, "dash/presets.html", {**nav_context("dash:presets"), "editor": view})


@require_loopback_or_staff
@require_POST
def preset_entry(request: "HttpRequest") -> "HttpResponse":
    """POST one loop's tri-state opinion within a preset (``on`` / ``off`` / ``inherit``)."""
    preset = request.POST.get("preset", "").strip()
    loop = request.POST.get("loop", "").strip()
    state = request.POST.get("state", "").strip()
    try:
        set_preset_entry(preset, loop, state)
    except PresetEditError as exc:
        return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action="preset:entry", target=f"{preset}/{loop}", after=state)
    return _back_to_preset(preset)


@require_loopback_or_staff
@require_POST
def preset_use(request: "HttpRequest") -> "HttpResponse":
    """POST the active preset override — activate one by name, or ``auto`` to clear it."""
    name = request.POST.get("preset", "").strip()
    try:
        if name == "auto":
            clear_preset_override(user_id=actor(request))
        else:
            activate_preset(name, hold=True, reason="dashboard", user_id=actor(request))
    except PresetEditError as exc:
        return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action="preset:use", after=name)
    return _back_to_preset("" if name == "auto" else name)


@require_loopback_or_staff
@require_POST
def preset_create(request: "HttpRequest") -> "HttpResponse":
    """POST a new, opinion-free preset."""
    name = request.POST.get("name", "").strip()
    try:
        create_preset(name, description=request.POST.get("description", "").strip())
    except PresetEditError as exc:
        return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action="preset:create", target=name)
    return _back_to_preset(name)


@require_loopback_or_staff
@require_POST
def preset_meta(request: "HttpRequest") -> "HttpResponse":
    """POST a preset's description and availability pin — an empty pin CLEARS it."""
    name = request.POST.get("preset", "").strip()
    try:
        update_preset_meta(
            name,
            description=request.POST.get("description", ""),
            availability_pin=request.POST.get("availability_pin", ""),
        )
    except PresetEditError as exc:
        return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action="preset:meta", target=name)
    return _back_to_preset(name)


@require_loopback_or_staff
@require_POST
def preset_rename(request: "HttpRequest") -> "HttpResponse":
    """POST a preset rename — the seam re-points every by-name referrer atomically."""
    name = request.POST.get("preset", "").strip()
    new_name = request.POST.get("new_name", "").strip()
    try:
        rename_preset(name, new_name)
    except PresetEditError as exc:
        return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action="preset:rename", target=name, after=new_name)
    return _back_to_preset(new_name)


@require_loopback_or_staff
@require_POST
def preset_delete(request: "HttpRequest") -> "HttpResponse":
    """POST a preset deletion — refused while anything still resolves it by name."""
    name = request.POST.get("preset", "").strip()
    try:
        delete_preset(name)
    except PresetEditError as exc:
        return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action="preset:delete", target=name)
    return redirect("dash:presets")


@require_loopback_or_staff
@require_POST
def schedule_activate(request: "HttpRequest") -> "HttpResponse":
    """POST the active schedule — switch calendars, or ``none`` to leave no L2 layer."""
    name = request.POST.get("schedule", "").strip()
    try:
        if name == "none":
            clear_active_schedule()
        else:
            set_active_schedule(name)
    except PresetEditError as exc:
        return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action="schedule:set-active", after=name)
    return redirect("dash:presets")


@require_loopback_or_staff
@require_POST
def schedule_slot(request: "HttpRequest") -> "HttpResponse":
    """POST a schedule slot — create when ``slot_id`` is absent, else update in place."""
    schedule = request.POST.get("schedule", "").strip()
    raw_slot_id = request.POST.get("slot_id", "").strip()
    try:
        upsert_schedule_slot(
            schedule,
            slot_id=int(raw_slot_id) if raw_slot_id else None,
            days=[int(day) for day in request.POST.getlist("days")],
            start_time=request.POST.get("start_time", "").strip(),
            preset_name=request.POST.get("preset", "").strip(),
        )
    except (PresetEditError, ValueError) as exc:
        return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action="schedule:slot", target=schedule, after=raw_slot_id or "new")
    return redirect("dash:presets")


@require_loopback_or_staff
@require_POST
def schedule_slot_delete(request: "HttpRequest") -> "HttpResponse":
    """POST the removal of one schedule slot."""
    schedule = request.POST.get("schedule", "").strip()
    raw_slot_id = request.POST.get("slot_id", "").strip()
    try:
        delete_schedule_slot(schedule, int(raw_slot_id))
    except (PresetEditError, ValueError) as exc:
        return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action="schedule:slot-delete", target=schedule, after=raw_slot_id)
    return redirect("dash:presets")


def _back_to_preset(name: str) -> "HttpResponse":
    """Redirect to the editor, keeping the edited preset's tab open."""
    target = redirect("dash:presets")
    if name:
        target["Location"] = f"{target['Location']}?preset={name}"
    return target
