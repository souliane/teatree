"""The import/export page and its two endpoints — the config transfer surface (#4340).

Its own page rather than a band on the settings page, because the dump reaches past the
settings store into the loop, preset and schedule rows: from the settings page a reader
could not tell that pressing Export also captures loop enablement, preset entries and the
weekly schedule — nor that importing writes them back, which is a materially bigger action
than "restore my settings" reads as. So the page STATES its scope
(:data:`~teatree.core.config_interchange.scope.EXPORT_SECTIONS`) at the point of use, and a
preview names the sections an apply would touch.

Coordinate only: parse the POST, hand it to the interchange seam, answer. Export withholds
secrets and offers the page's two filters; import takes an uploaded file and previews with a
dry run before an explicit apply.
"""

import tomllib
from typing import TYPE_CHECKING, NotRequired

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from teatree.core.config_interchange.migration import import_toml_to_db
from teatree.core.config_interchange.scope import EXPORT_SECTIONS, ExportSection
from teatree.core.config_interchange.types import ConfigImport
from teatree.dash import audit
from teatree.dash.interchange import SectionChange, changed_sections, export_text, import_preview
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import SAFETY_CONFIRM_PHRASE, NavContext, actor, nav_context

if TYPE_CHECKING:
    from django.http import HttpRequest

#: The largest import file the page accepts. A shipped ``defaults.toml`` is ~20KB, so this
#: is generous for any real config while refusing a mis-picked archive outright.
MAX_IMPORT_BYTES = 1_000_000


class InterchangeContext(NavContext):
    sections: tuple[ExportSection, ...]
    confirm_phrase: str
    changed_sections: tuple[SectionChange, ...]
    # Present only on the answer to an import POST.
    import_result: NotRequired[ConfigImport]
    import_applied: NotRequired[bool]
    import_error: NotRequired[str]


def _page_context() -> InterchangeContext:
    nav = nav_context("dash:interchange")
    return {
        "nav_items": nav["nav_items"],
        "nav_active": nav["nav_active"],
        "instance_label": nav["instance_label"],
        "sections": EXPORT_SECTIONS,
        "confirm_phrase": SAFETY_CONFIRM_PHRASE,
        "changed_sections": (),
    }


@require_loopback_or_staff
@require_GET
def interchange(request: "HttpRequest") -> "HttpResponse":
    """The transfer page — what the dump covers, then the export and import controls."""
    return render(request, "dash/interchange.html", _page_context())


@require_loopback_or_staff
@require_GET
def interchange_export(request: "HttpRequest") -> "HttpResponse":
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


def _refused(request: "HttpRequest", reason: str) -> "HttpResponse":
    context = _page_context()
    context["import_error"] = reason
    return render(request, "dash/interchange.html", context, status=400)


@require_loopback_or_staff
@require_POST
def interchange_import(request: "HttpRequest") -> "HttpResponse":
    """POST an uploaded TOML file — a dry-run preview by default, a write only with ``apply``.

    A rejected row refuses the whole import (Phase-4 atomicity); the result rides back onto
    the page — with the sections it touches named — so the operator sees exactly what changed
    (or would change), across which families, and what was refused.

    A safety-posture key needs the SAME typed confirm phrase ``settings_set`` demands — an
    uploaded dump is not a per-key intent. The preview always classifies as if authorized, so
    the operator SEES which rows are safety-posture (each flagged) before deciding; the apply
    passes the authorization only when the phrase is present, and without it the import is
    refused wholesale with the offending key named.
    """
    text, upload_error = _uploaded_toml(request)
    if upload_error:
        return _refused(request, upload_error)
    confirmed = request.POST.get("confirm", "").strip() == SAFETY_CONFIRM_PHRASE
    try:
        preview = import_preview(text)
    except tomllib.TOMLDecodeError as exc:
        return _refused(request, f"invalid TOML: {exc}")
    apply_now = request.POST.get("apply", "").strip() == "1" and not preview.rejected
    result = import_toml_to_db(text, allow_safety_posture=confirmed) if apply_now else preview
    written = apply_now and not result.rejected
    if written:
        audit.record(actor=actor(request), action="settings:import", after=f"{len(result.written)} row(s)")
    context = _page_context()
    context["import_result"] = result
    context["import_applied"] = written
    context["changed_sections"] = changed_sections(result)
    return render(request, "dash/interchange.html", context)
