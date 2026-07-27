"""The model-driven settings-editor POSTs — list/edit every config key from the dashboard (D7).

Coordinate only: parse the POST, write through ``ConfigSetting.set_value`` (the same seam
``config_setting set`` uses, so the #258 coercion + #3688 cross-key checks fire), audit,
answer. Restore-to-default deletes the row; a safety-posture key needs the extra confirm
phrase; import previews with a dry-run before an explicit apply. A secret's value never
enters the page — the editor surface masks it before the context is built.

The answer is the edited ROW for an htmx request (the browser swaps that one ``<tr>``, so
the scroll position never moves) and the pre-htmx redirect otherwise, keeping the page
usable with JavaScript off. Both paths run the identical write and audit first.
"""

import json
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from teatree.config import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.config.write_validation import ConfigWriteError, validate_config_write
from teatree.core.config_display import is_secret
from teatree.core.config_migration import import_toml_to_db
from teatree.core.models import ConfigSetting
from teatree.dash import audit
from teatree.dash.settings_editor import build_setting_row, build_settings_editor, export_text, import_preview
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import actor, nav_context

if TYPE_CHECKING:
    from django.http import HttpRequest

#: The phrase an operator must type to change a safety-posture key (write-is-authorization).
SAFETY_CONFIRM_PHRASE = "change-safety-posture"


def _audit_after(key: str, canonical: object) -> str:
    """The audit ``after`` value — never the real value of a secret key."""
    return "***" if is_secret(key) else str(canonical)


def _row_fragment(request: "HttpRequest", key: str, scope: str, *, error: str = "", status: int = 200) -> HttpResponse:
    """Re-read *key* and answer its row alone — the htmx swap unit."""
    context = {
        "s": build_setting_row(key, scope),
        "scope": scope,
        "confirm_phrase": SAFETY_CONFIRM_PHRASE,
        "row_error": error,
    }
    return render(request, "dash/partials/_settings_row.html", context, status=status)


def _refused(request: "HttpRequest", key: str, scope: str, reason: str) -> HttpResponse:
    """A refused write — the row carrying its reason for htmx, the plain 400 body otherwise."""
    if request.headers.get("HX-Request") != "true":
        return HttpResponseBadRequest(reason)
    return _row_fragment(request, key, scope, error=reason, status=400)


def _written(request: "HttpRequest", key: str, scope: str) -> HttpResponse:
    """A landed write — the refreshed row for htmx, the pre-htmx redirect otherwise."""
    if request.headers.get("HX-Request") != "true":
        return _back(scope)
    return _row_fragment(request, key, scope)


@require_loopback_or_staff
@require_GET
def settings(request: "HttpRequest") -> "HttpResponse":
    """The full model-driven editor — every schema key, secret values masked."""
    scope = request.GET.get("scope", "").strip()
    view = build_settings_editor(scope)
    context = {**nav_context("dash:settings"), "editor": view, "confirm_phrase": SAFETY_CONFIRM_PHRASE}
    return render(request, "dash/settings.html", context)


@require_loopback_or_staff
@require_POST
def settings_set(request: "HttpRequest") -> "HttpResponse":
    """POST one setting → the DB store, through the validating ``set_value`` seam."""
    key = request.POST.get("key", "").strip()
    scope = request.POST.get("scope", "").strip()
    if key not in ALL_KNOWN_CONFIG_SETTINGS:
        # No row exists to swap for a key the schema does not know — plain refusal either way.
        return HttpResponseBadRequest(f"unknown setting {key!r}")
    if key in SAFETY_POSTURE_KEYS and request.POST.get("confirm", "").strip() != SAFETY_CONFIRM_PHRASE:
        return _refused(request, key, scope, f"a safety-posture key needs the confirm phrase {SAFETY_CONFIRM_PHRASE!r}")
    try:
        parsed = json.loads(request.POST.get("value", ""))
    except json.JSONDecodeError as exc:
        return _refused(request, key, scope, f"invalid JSON value: {exc}")
    try:
        canonical = validate_config_write(key, parsed)
    except ConfigWriteError as exc:
        return _refused(request, key, scope, f"invalid value for {key}: {exc}")
    try:
        ConfigSetting.objects.set_value(key, canonical, scope=scope)
    except ValidationError as exc:
        return _refused(request, key, scope, f"inconsistent config for {key}: {exc.messages[0]}")
    audit.record(actor=actor(request), action="settings:set", target=key, after=_audit_after(key, canonical))
    return _written(request, key, scope)


@require_loopback_or_staff
@require_POST
def settings_restore(request: "HttpRequest") -> "HttpResponse":
    """POST a restore-to-default — DELETE the DB row so the setting resolves its default again."""
    key = request.POST.get("key", "").strip()
    scope = request.POST.get("scope", "").strip()
    if key not in ALL_KNOWN_CONFIG_SETTINGS:
        return HttpResponseBadRequest(f"unknown setting {key!r}")
    if ConfigSetting.objects.clear(key, scope=scope):
        audit.record(actor=actor(request), action="settings:restore", target=key)
    return _written(request, key, scope)


@require_loopback_or_staff
@require_GET
def settings_export(_request: "HttpRequest") -> "HttpResponse":
    """Download the shareable export — secrets withheld, personal included."""
    response = HttpResponse(export_text(), content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="teatree-config.toml"'
    return response


@require_loopback_or_staff
@require_POST
def settings_import(request: "HttpRequest") -> "HttpResponse":
    """POST an import — a dry-run preview by default, an actual write only with ``apply``.

    A rejected row refuses the whole import (Phase-4 atomicity); the result rides back onto
    the page so the operator sees exactly what changed (or would change) and what was refused.

    A safety-posture key needs the SAME typed confirm phrase ``settings_set`` demands — a
    paste is not a per-key intent. The preview always classifies as if authorized, so the
    operator SEES which rows are safety-posture (each flagged) before deciding; the apply
    passes the authorization only when the phrase is present, and without it the import is
    refused wholesale with the offending key named.
    """
    text = request.POST.get("toml", "")
    confirmed = request.POST.get("confirm", "").strip() == SAFETY_CONFIRM_PHRASE
    preview = import_preview(text)
    apply_now = request.POST.get("apply", "").strip() == "1" and not preview.rejected
    result = import_toml_to_db(text, allow_safety_posture=confirmed) if apply_now else preview
    written = apply_now and not result.rejected
    if written:
        audit.record(actor=actor(request), action="settings:import", after=f"{len(result.written)} row(s)")
    context = {
        **nav_context("dash:settings"),
        "editor": build_settings_editor(),
        "confirm_phrase": SAFETY_CONFIRM_PHRASE,
        "import_result": result,
        "import_applied": written,
    }
    return render(request, "dash/settings.html", context)


def _back(scope: str) -> "HttpResponse":
    """Redirect to the editor, keeping the edited scope selected."""
    target = redirect("dash:settings")
    if scope:
        target["Location"] = f"{target['Location']}?scope={scope}"
    return target
