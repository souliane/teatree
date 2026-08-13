"""The loop-control surface: per-loop verbs, mode switch, gate toggle (#3162).

Every mutation POSTs through here CSRF-protected (Django's ``CsrfViewMiddleware``
guards these unexempted views), drives the SAME manager/override chokepoints the
CLI uses (never a raw field write), and records one audit line.
"""

from typing import TYPE_CHECKING, TypedDict

from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from teatree.core.mode_resolution import clear_mode_override, set_mode_override
from teatree.core.models.config_setting import ConfigSetting
from teatree.dash import audit
from teatree.dash.loop_control import (
    GATE_CONFIRM_PHRASE,
    MODE_SWITCH_AUTO,
    RUNNER_CONFIRM_PHRASE,
    LoopActionError,
    LoopControlView,
    apply_loop_action,
    build_loop_control,
)
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import actor, error_page, is_htmx, nav_context
from teatree.loops.loop_cadence_editing import CadenceEditError, set_loop_cadence

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

_GATE_KEY = "danger_gate_fail_open"
_RUNNER_KEY = "loop_runner_enabled"


class LoopsContext(TypedDict):
    control: LoopControlView
    gate_confirm_phrase: str
    runner_confirm_phrase: str


def _answer(request: "HttpRequest", *, error: str = "") -> "HttpResponse":
    """The page body for an htmx request, the pre-htmx redirect (or error page) otherwise.

    Every mutation here used to end in ``redirect("dash:loops")``, so acting on a loop
    re-rendered the whole document and jumped to scroll 0. The body carries the header
    bands AND the polled table, because the posture switch and both kill switches
    can change a loop's effective verdict.
    """
    if not is_htmx(request):
        return error_page(request, error, back="dash:loops") if error else redirect("dash:loops")
    context = {**_loops_context(), "page_error": error}
    return render(request, "dash/partials/_loops_body.html", context, status=400 if error else 200)


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
        return _answer(request, error=str(exc))
    audit.record(actor=actor(request), action=f"loop:{action}", target=name, after=landed)
    return _answer(request)


@require_loopback_or_staff
@require_POST
def mode_switch(request: "HttpRequest") -> "HttpResponse":
    """POST a mode switch through the mode-override chokepoint (#61, #3826).

    The switch names a ``Mode`` row (or ``auto`` to clear the override) and sets it via
    :func:`teatree.core.mode_resolution.set_mode_override` / :func:`clear_mode_override`,
    the same chokepoint the ``t3 loop preset`` CLI uses. An unknown name is refused
    there rather than written as an override that falls open to base config.
    """
    switch = request.POST.get("mode", "").strip()
    if switch == MODE_SWITCH_AUTO:
        clear_mode_override()
    else:
        try:
            set_mode_override(switch)
        except LookupError as exc:
            return _answer(request, error=str(exc))
    audit.record(actor=actor(request), action="mode", after=switch)
    return _answer(request)


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
        return _answer(request, error=f"type {GATE_CONFIRM_PHRASE!r} to enable fail-open")
    before = str(ConfigSetting.objects.get_effective(_GATE_KEY))
    ConfigSetting.objects.set_value(_GATE_KEY, value=enable)
    audit.record(actor=actor(request), action="gate:danger_gate_fail_open", before=before, after=str(enable))
    return _answer(request)


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
        return _answer(request, error=f"type {RUNNER_CONFIRM_PHRASE!r} to stop the loop fleet")
    before = str(ConfigSetting.objects.get_effective(_RUNNER_KEY))
    ConfigSetting.objects.set_value(_RUNNER_KEY, value=enable)
    audit.record(actor=actor(request), action=f"kill-switch:{_RUNNER_KEY}", before=before, after=str(enable))
    return _answer(request)


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
        return _answer(request, error=str(exc))
    audit.record(actor=actor(request), action="loop:cadence", target=name, after=landed.cadence_label)
    return _answer(request)
