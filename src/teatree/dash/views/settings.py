"""The ONE settings page and its POSTs — every config key, plus the live readouts (D7, #3664).

Coordinate only: parse the POST, write through ``ConfigSetting.set_value`` (the same seam
``config_setting set`` uses, so the #258 coercion + #3688 cross-key checks fire), audit,
answer. Restore-to-default deletes the row; a safety-posture key needs the extra confirm
phrase. A secret's value never enters the page — the editor surface masks it before the
context is built.

Transferring the whole store is :mod:`teatree.dash.views.interchange`'s page, not this one:
a dump reaches past the settings store into the loop, preset and schedule rows, so hosting
that control here under-stated what it does (#4340).

**One section per request.** The page is a left nav of sections and a right pane; selecting
a section ``hx-get``s :func:`settings_group` for that section alone. Rendering every key at
once produced a 260KB, 14,000px page carrying 272 forms and 1,060 inputs, most of them
hidden fields and a CSRF token repeated on every row. The page now carries ONE CSRF token
(the body's ``hx-headers`` in ``base.html``, the pattern the terminal button already uses)
and each row's ``key`` and ``scope`` ride in its ``hx-post`` URL instead of hidden inputs.

The retired ``/dash/config`` page is absorbed here: its resolved model / credential /
self-repair readouts keep their own 15s htmx poll (:func:`settings_readouts`) beside the
editable rows, and every dial it rendered read-only is now an editable row instead.

The answer to a write is the edited ROW for an htmx request — the browser swaps that one
``<tr>``, so an edit never re-renders the document and the scroll position never moves —
and a redirect otherwise. Both paths run the identical write and audit first.
"""

import json
from typing import TYPE_CHECKING, TypedDict

from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from teatree.config import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.config.write_validation import ConfigWriteError, validate_config_write
from teatree.core.config_display import is_secret
from teatree.core.models import ConfigSetting
from teatree.dash import audit
from teatree.dash.settings_editor import (
    SettingsEditorView,
    SettingsGroupView,
    build_setting_row,
    build_settings_editor,
    build_settings_group,
)
from teatree.dash.settings_readouts import ReadoutsView, build_readouts_view
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import SAFETY_CONFIRM_PHRASE, NavContext, actor, nav_context

if TYPE_CHECKING:
    from django.http import HttpRequest


class ReadoutsContext(TypedDict):
    readouts: ReadoutsView


class SettingsPageContext(NavContext, ReadoutsContext):
    editor: SettingsEditorView
    confirm_phrase: str


class SettingsGroupContext(TypedDict):
    """The right pane on its own — the htmx target of a section click."""

    group: SettingsGroupView
    confirm_phrase: str


def _readouts_context() -> ReadoutsContext:
    return {"readouts": build_readouts_view()}


def _page_context(section: str = "") -> SettingsPageContext:
    """The whole settings page — nav, the selected section's rows, and the live readouts."""
    nav = nav_context("dash:settings")
    return {
        "nav_items": nav["nav_items"],
        "nav_active": nav["nav_active"],
        "instance_label": nav["instance_label"],
        "readouts": build_readouts_view(),
        "editor": build_settings_editor(section),
        "confirm_phrase": SAFETY_CONFIRM_PHRASE,
    }


def _audit_after(key: str, canonical: object) -> str:
    """The audit ``after`` value — never the real value of a secret key."""
    return "***" if is_secret(key) else str(canonical)


def _row_fragment(request: "HttpRequest", key: str, *, error: str = "", status: int = 200) -> HttpResponse:
    """Re-read *key* across every scope and answer its row alone — the htmx swap unit."""
    context = {
        "s": build_setting_row(key),
        "confirm_phrase": SAFETY_CONFIRM_PHRASE,
        "row_error": error,
    }
    return render(request, "dash/partials/_settings_row.html", context, status=status)


def _refused(request: "HttpRequest", key: str, reason: str) -> HttpResponse:
    """A refused write — the row carrying its reason for htmx, the plain 400 body otherwise.

    The row is re-read before it goes back, so the cell shows what is actually stored rather
    than the typed text that was refused: a failed write can never look saved.
    """
    if request.headers.get("HX-Request") != "true":
        return HttpResponseBadRequest(reason)
    return _row_fragment(request, key, error=reason, status=400)


def _written(request: "HttpRequest", key: str) -> HttpResponse:
    """A landed write — the refreshed row for htmx, the pre-htmx redirect otherwise."""
    if request.headers.get("HX-Request") != "true":
        return _back()
    return _row_fragment(request, key)


@require_loopback_or_staff
@require_GET
def settings(request: "HttpRequest") -> "HttpResponse":
    """The one settings page — the section nav, the selected section's grid, the readouts."""
    return render(request, "dash/settings.html", _page_context(request.GET.get("section", "").strip()))


@require_loopback_or_staff
@require_GET
def settings_group(request: "HttpRequest", slug: str) -> "HttpResponse":
    """One section's rows — the right pane, and the whole cost of switching sections."""
    context: SettingsGroupContext = {
        "group": build_settings_group(slug),
        "confirm_phrase": SAFETY_CONFIRM_PHRASE,
    }
    return render(request, "dash/partials/_settings_group.html", context)


@require_loopback_or_staff
@require_GET
def settings_readouts(request: "HttpRequest") -> "HttpResponse":
    """The resolved model / credential / self-repair readouts — the target of the htmx poll."""
    return render(request, "dash/partials/_settings_readouts.html", _readouts_context())


@require_loopback_or_staff
@require_POST
def settings_set(request: "HttpRequest", key: str) -> "HttpResponse":
    """POST one cell → the DB store, through the validating ``set_value`` seam.

    *key* rides in the URL and the edited cell's scope in its query string, so a row carries
    no hidden inputs — the reduction that took 812 hidden fields off the page.

    An EMPTY submitted value clears the scope's row, so the setting resolves its default
    again. With click-to-edit that IS the restore gesture: emptying the cell and changing it
    are the same interaction, which is why the row carries no separate restore control.
    """
    scope = request.GET.get("scope", "").strip()
    if key not in ALL_KNOWN_CONFIG_SETTINGS:
        # No row exists to swap for a key the schema does not know — plain refusal either way.
        return HttpResponseBadRequest(f"unknown setting {key!r}")
    if key in SAFETY_POSTURE_KEYS and request.POST.get("confirm", "").strip() != SAFETY_CONFIRM_PHRASE:
        return _refused(request, key, f"a safety-posture key needs the confirm phrase {SAFETY_CONFIRM_PHRASE!r}")
    submitted = request.POST.get("value", "").strip()
    if not submitted:
        return _cleared(request, key, scope)
    refusal, canonical = _stored(key, submitted, scope)
    if refusal:
        return _refused(request, key, refusal)
    audit.record(actor=actor(request), action="settings:set", target=key, after=_audit_after(key, canonical))
    return _written(request, key)


def _stored(key: str, submitted: str, scope: str) -> tuple[str, object]:
    """Parse, validate and WRITE *submitted*; ``("", canonical)`` or ``(reason, None)``.

    The three ways a cell's text is refused answer identically — the row comes back carrying
    the reason — so they are decided here and the view is left with one branch.
    """
    try:
        parsed = json.loads(submitted)
    except json.JSONDecodeError as exc:
        return f"invalid JSON value: {exc}", None
    try:
        canonical = validate_config_write(key, parsed)
    except ConfigWriteError as exc:
        return f"invalid value for {key}: {exc}", None
    try:
        ConfigSetting.objects.set_value(key, canonical, scope=scope)
    except ValidationError as exc:
        return f"inconsistent config for {key}: {exc.messages[0]}", None
    return "", canonical


def _cleared(request: "HttpRequest", key: str, scope: str) -> "HttpResponse":
    """Clear *key*'s row in *scope* — the emptied-cell half of the click-to-edit gesture."""
    if ConfigSetting.objects.clear(key, scope=scope):
        audit.record(actor=actor(request), action="settings:restore", target=key)
    return _written(request, key)


@require_loopback_or_staff
@require_POST
def settings_restore(request: "HttpRequest", key: str) -> "HttpResponse":
    """POST a restore-to-default — DELETE the DB row so the setting resolves its default again."""
    scope = request.GET.get("scope", "").strip()
    if key not in ALL_KNOWN_CONFIG_SETTINGS:
        return HttpResponseBadRequest(f"unknown setting {key!r}")
    return _cleared(request, key, scope)


def _back() -> "HttpResponse":
    """Redirect back to the grid — the answer to a write that did not come through htmx."""
    return redirect("dash:settings")
