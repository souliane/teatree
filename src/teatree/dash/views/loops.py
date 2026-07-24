"""The loop-control surface: per-loop verbs, availability switch, gate toggle (#3162).

Every mutation POSTs through here CSRF-protected (Django's ``CsrfViewMiddleware``
guards these unexempted views), drives the SAME manager/override chokepoints the
CLI uses (never a raw field write), and records one audit line.
"""

from typing import TYPE_CHECKING, TypedDict

from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from teatree.core.mode_resolution import clear_mode_override, mode_name_for_availability, set_mode_override
from teatree.core.models.config_setting import ConfigSetting
from teatree.dash import audit
from teatree.dash.loop_control import (
    AVAILABILITY_ACTIONS,
    GATE_CONFIRM_PHRASE,
    RUNNER_CONFIRM_PHRASE,
    LoopActionError,
    LoopControlView,
    apply_loop_action,
    build_loop_control,
)
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import actor, nav_context
from teatree.loops.loop_cadence_editing import CadenceEditError, set_loop_cadence

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

_GATE_KEY = "danger_gate_fail_open"
_RUNNER_KEY = "loop_runner_enabled"


class LoopsContext(TypedDict):
    control: LoopControlView
    gate_confirm_phrase: str
    runner_confirm_phrase: str


def _loops_context() -> LoopsContext:
    return {
        "control": build_loop_control(),
        "gate_confirm_phrase": GATE_CONFIRM_PHRASE,
        "runner_confirm_phrase": RUNNER_CONFIRM_PHRASE,
    }


@require_loopback_or_staff
@require_GET
def loops(request: "HttpRequest") -> "HttpResponse":
    """Full loop-control page — every loop's effective verdict + the header controls."""
    context = {**nav_context("dash:loops"), **_loops_context()}
    return render(request, "dash/loops.html", context)


@require_loopback_or_staff
@require_GET
def loops_table_partial(request: "HttpRequest") -> "HttpResponse":
    """The loop table fragment — the target of the htmx poll."""
    return render(request, "dash/partials/_loops_table.html", _loops_context())


@require_loopback_or_staff
@require_POST
def loop_action(request: "HttpRequest") -> "HttpResponse":
    """POST a per-loop control verb (pause / resume / disable / enable)."""
    name = request.POST.get("name", "").strip()
    action = request.POST.get("action", "").strip()
    try:
        landed = apply_loop_action(action, name)
    except LoopActionError as exc:
        return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action=f"loop:{action}", target=name, after=landed)
    return redirect("dash:loops")


@require_loopback_or_staff
@require_POST
def availability(request: "HttpRequest") -> "HttpResponse":
    """POST an availability switch through the merged mode-override chokepoint (#61).

    The standalone availability modes are gone: each switch resolves the mode
    carrying that posture BY ROW and sets (or clears) a ``ModeOverride`` via
    :func:`teatree.core.mode_resolution.set_mode_override` /
    :func:`clear_mode_override`, keeping the return-to-reachable deferred-question
    drain firing exactly like the ``t3 loop preset`` CLI.
    """
    mode = request.POST.get("mode", "").strip()
    if mode not in AVAILABILITY_ACTIONS:
        return HttpResponseBadRequest(f"unknown availability mode {mode!r}")
    if mode == "auto":
        clear_mode_override(user_id=actor(request))
    else:
        try:
            set_mode_override(mode_name_for_availability(mode), user_id=actor(request))
        except LookupError as exc:
            return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action="availability", after=mode)
    return redirect("dash:loops")


@require_loopback_or_staff
@require_POST
def gate_toggle(request: "HttpRequest") -> "HttpResponse":
    """POST the ``danger_gate_fail_open`` master switch, gated behind a typed confirm.

    Turning fail-open ON relaxes every over-deny gate, so it requires typing the
    exact confirm phrase — never a one-click toggle. Both directions are audited.
    """
    enable = request.POST.get("enable") in {"1", "true", "on"}
    confirm = request.POST.get("confirm", "").strip()
    if enable and confirm != GATE_CONFIRM_PHRASE:
        return HttpResponseBadRequest(f"type {GATE_CONFIRM_PHRASE!r} to enable fail-open")
    before = str(ConfigSetting.objects.get_effective(_GATE_KEY))
    ConfigSetting.objects.set_value(_GATE_KEY, value=enable)
    audit.record(actor=actor(request), action="gate:danger_gate_fail_open", before=before, after=str(enable))
    return redirect("dash:loops")


@require_loopback_or_staff
@require_POST
def runner_toggle(request: "HttpRequest") -> "HttpResponse":
    """POST the global ``loop_runner_enabled`` kill-switch, gated behind a typed confirm.

    Turning it OFF stops the whole loop fleet, and an accidental stop is the
    hardest flip on this page to notice — nothing errors, work simply stops
    arriving. So the OFF direction requires typing the exact confirm phrase, ON
    does not (restarting the fleet is recoverable), and BOTH are audited.
    """
    enable = request.POST.get("enable") in {"1", "true", "on"}
    confirm = request.POST.get("confirm", "").strip()
    if not enable and confirm != RUNNER_CONFIRM_PHRASE:
        return HttpResponseBadRequest(f"type {RUNNER_CONFIRM_PHRASE!r} to stop the loop fleet")
    before = str(ConfigSetting.objects.get_effective(_RUNNER_KEY))
    ConfigSetting.objects.set_value(_RUNNER_KEY, value=enable)
    audit.record(actor=actor(request), action=f"kill-switch:{_RUNNER_KEY}", before=before, after=str(enable))
    return redirect("dash:loops")


@require_loopback_or_staff
@require_POST
def loop_cadence(request: "HttpRequest") -> "HttpResponse":
    """POST a loop's cadence — an interval XOR a wall-clock time, via the validated seam.

    The form submits whichever field the loop's cadence mode uses; the seam owns
    the XOR and the per-loop bounds (a registry-floor loop may not be slowed past
    its declared floor), so an out-of-bounds value is refused rather than written.
    """
    name = request.POST.get("name", "").strip()
    raw_interval = request.POST.get("delay_seconds", "").strip()
    try:
        landed = set_loop_cadence(
            name,
            delay_seconds=int(raw_interval) if raw_interval else None,
            daily_at=request.POST.get("daily_at", "").strip(),
        )
    except (CadenceEditError, ValueError) as exc:
        return HttpResponseBadRequest(str(exc))
    audit.record(actor=actor(request), action="loop:cadence", target=name, after=landed.cadence_label)
    return redirect("dash:loops")
