"""The ONE settings page and its POSTs — every config key, plus the live readouts (D7, #3664).

Coordinate only: parse the POST, write through ``ConfigSetting.set_value`` (the same seam
``config_setting set`` uses, so the #258 coercion + #3688 cross-key checks fire), audit,
answer. Restore-to-default deletes the row; a safety-posture key needs the extra confirm
phrase; import takes an uploaded file and previews with a dry-run before an explicit apply.
A secret's value never enters the page — the editor surface masks it before the context is
built.

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
import tomllib
from typing import TYPE_CHECKING, NotRequired, TypedDict

from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from teatree.config import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.config.write_validation import ConfigWriteError, validate_config_write
from teatree.core.config_display import is_secret
from teatree.core.config_migration import ConfigImport, import_toml_to_db
from teatree.core.models import ConfigSetting
from teatree.dash import audit
from teatree.dash.settings_editor import (
    SettingsEditorView,
    SettingsGroupView,
    build_setting_row,
    build_settings_editor,
    build_settings_group,
    export_text,
    import_preview,
)
from teatree.dash.settings_readouts import ReadoutsView, build_readouts_view
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import NavContext, actor, nav_context

if TYPE_CHECKING:
    from django.http import HttpRequest

#: The phrase an operator must type to change a safety-posture key (write-is-authorization).
SAFETY_CONFIRM_PHRASE = "change-safety-posture"

#: The largest import file the page accepts. A shipped ``defaults.toml`` is ~20KB, so this
#: is generous for any real config while refusing a mis-picked archive outright.
MAX_IMPORT_BYTES = 1_000_000


class ReadoutsContext(TypedDict):
    readouts: ReadoutsView


class SettingsPageContext(NavContext, ReadoutsContext):
    editor: SettingsEditorView
    confirm_phrase: str
    # Present only on the answer to an import POST, which re-renders the whole page.
    import_result: NotRequired[ConfigImport]
    import_applied: NotRequired[bool]
    import_error: NotRequired[str]


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


@require_loopback_or_staff
@require_GET
def settings_export(request: "HttpRequest") -> "HttpResponse":
    """Download the export — secrets withheld, and the two filters the page offers.

    ``default_keys_only`` + ``include_defaults`` are the page's checkboxes, both unticked by
    default. Ticking both downloads the ``defaults.toml`` shape, so the file the operator
    gets is a drop-in replacement for the shipped one rather than a fragment of it.
    """
    default_keys_only = request.GET.get("default_keys_only") == "1"
    include_defaults = request.GET.get("include_defaults") == "1"
    text = export_text(default_keys_only=default_keys_only, include_defaults=include_defaults)
    filename = "defaults.toml" if default_keys_only and include_defaults else "teatree-config.toml"
    response = HttpResponse(text, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _uploaded_toml(request: "HttpRequest") -> tuple[str, str]:
    """The uploaded file's text, or ``("", reason)`` when there is nothing usable to import.

    An upload beats a paste: the operator picks the file they exported rather than shuttling
    two hundred keys through a textarea. A non-UTF-8 or oversized file is refused HERE, with
    a reason, rather than reaching the parser as mojibake.
    """
    upload = request.FILES.get("toml_file")
    if upload is None:
        return "", "choose a .toml file to import"
    if upload.size > MAX_IMPORT_BYTES:
        return "", f"that file is {upload.size} bytes — the import limit is {MAX_IMPORT_BYTES}"
    try:
        return upload.read().decode("utf-8"), ""
    except UnicodeDecodeError:
        return "", "that file is not UTF-8 text — export a TOML dump and upload that"


@require_loopback_or_staff
@require_POST
def settings_import(request: "HttpRequest") -> "HttpResponse":
    """POST an uploaded TOML file — a dry-run preview by default, a write only with ``apply``.

    A rejected row refuses the whole import (Phase-4 atomicity); the result rides back onto
    the page so the operator sees exactly what changed (or would change) and what was refused.

    A safety-posture key needs the SAME typed confirm phrase ``settings_set`` demands — an
    uploaded dump is not a per-key intent. The preview always classifies as if authorized, so
    the operator SEES which rows are safety-posture (each flagged) before deciding; the apply
    passes the authorization only when the phrase is present, and without it the import is
    refused wholesale with the offending key named.
    """
    text, upload_error = _uploaded_toml(request)
    if upload_error:
        context = _page_context()
        context["import_error"] = upload_error
        return render(request, "dash/settings.html", context, status=400)
    confirmed = request.POST.get("confirm", "").strip() == SAFETY_CONFIRM_PHRASE
    try:
        preview = import_preview(text)
    except tomllib.TOMLDecodeError as exc:
        context = _page_context()
        context["import_error"] = f"invalid TOML: {exc}"
        return render(request, "dash/settings.html", context, status=400)
    apply_now = request.POST.get("apply", "").strip() == "1" and not preview.rejected
    result = import_toml_to_db(text, allow_safety_posture=confirmed) if apply_now else preview
    written = apply_now and not result.rejected
    if written:
        audit.record(actor=actor(request), action="settings:import", after=f"{len(result.written)} row(s)")
    context = _page_context()
    context["import_result"] = result
    context["import_applied"] = written
    return render(request, "dash/settings.html", context)


def _back() -> "HttpResponse":
    """Redirect back to the grid — the answer to a write that did not come through htmx."""
    return redirect("dash:settings")
