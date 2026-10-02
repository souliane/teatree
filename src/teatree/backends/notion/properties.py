"""Read and write ONE named page property, typed by the property itself.

``t3 notion fetch`` renders a page's BLOCK tree, which cannot answer "what is
this page's GitLab Reference?" — a property hangs off the page object, beside
the blocks rather than inside them. This module is that missing half, and the
only way a headless run polls a page for a value a human is expected to fill in.

**The caller never names a Notion type, so it can never name the wrong one.**
The write derives its payload shape from the property's OWN type on the live
page: the same ``--value BUG-23`` lands as a ``rich_text`` array on a text
property and as ``{"status": {"name": …}}`` on a status one.

**Values are literal.** A property is compared character for character by the
caller that polls it, so the text is stored verbatim rather than parsed as
Markdown.

A type with no faithful plain-text form, or a value the type cannot hold,
raises :class:`~teatree.backends.notion.errors.NotionUnwritablePropertyError` —
the same refusal :mod:`~teatree.backends.notion.blocks` makes for a Markdown
construct it will not guess a mapping for. Reading is total by contrast: a type
with no plain form renders as its raw payload rather than as ``""``, because an
empty string a caller then treats as "not set yet" is the one wrong answer a
poller cannot detect.
"""

import dataclasses
import datetime
import json
from collections.abc import Callable
from typing import cast

from teatree.backends.notion.blocks import literal_rich_text
from teatree.backends.notion.client import NotionClient, option_name
from teatree.backends.notion.errors import (
    NotionPropertyNotFoundError,
    NotionUnwritablePropertyError,
    NotionWriteNotLandedError,
)
from teatree.backends.notion.markdown import rich_text_plain
from teatree.types import RawAPIDict

_TRUE = frozenset({"true", "yes", "1", "on", "checked"})
_FALSE = frozenset({"false", "no", "0", "off", "unchecked", ""})


def page_property(page: RawAPIDict, name: str) -> RawAPIDict:
    """The named property object of a fetched *page*, or raise naming what IS there."""
    properties = page.get("properties")
    carried = cast("RawAPIDict", properties) if isinstance(properties, dict) else {}
    found = carried.get(name)
    if not isinstance(found, dict):
        msg = (
            f"page {page.get('id', '?')} carries no property named {name!r}. "
            f"Its properties are: {sorted(carried)}. Property names are case- and "
            "space-sensitive, and a database column absent from this page reads exactly like a typo."
        )
        raise NotionPropertyNotFoundError(msg)
    return cast("RawAPIDict", found)


def property_type(prop: RawAPIDict) -> str:
    return str(prop.get("type", ""))


def plain_property_value(prop: RawAPIDict) -> str:
    """Render any property to the text a headless caller branches on."""
    reader = _READERS.get(property_type(prop))
    if reader is None:
        return json.dumps(prop.get(property_type(prop)), sort_keys=True)
    return reader(prop)


@dataclasses.dataclass(frozen=True)
class PropertyWrite:
    """A property patch plus the plain value the re-read must show for it."""

    payload: RawAPIDict
    expected_plain: str


def build_property_write(prop: RawAPIDict, value: str) -> PropertyWrite:
    """Shape *value* for *prop*'s own type, refusing what that type cannot hold."""
    kind = property_type(prop)
    builder = _WRITERS.get(kind)
    if builder is None:
        msg = (
            f"a {kind!r} property cannot be set from plain text — Notion either computes it "
            "or needs object ids rather than names. Set it in the Notion UI, or point at a "
            f"property this surface can write: {sorted(_WRITERS)}."
        )
        raise NotionUnwritablePropertyError(msg)
    return builder(value)


@dataclasses.dataclass(frozen=True)
class PropertyWriteResult:
    """What a property write did, including what it replaced."""

    name: str
    type: str
    previous: str
    value: str


class PagePropertyWriter:
    """Set one named property, deriving the payload from its live type and verifying.

    Notion answers ``200`` on a page patch that changes nothing, so the write is
    followed by a re-read of the same property and refuses to report success
    unless the page now carries what the caller asked for.
    """

    def __init__(self, client: NotionClient) -> None:
        self._client = client

    def write(self, page_id: str, *, name: str, value: str) -> PropertyWriteResult:
        before = page_property(self._client.get_page(page_id), name)
        intended = build_property_write(before, value)
        self._client.update_page_properties(page_id, {name: intended.payload})
        after = page_property(self._client.get_page(page_id), name)
        landed = plain_property_value(after)
        if landed != intended.expected_plain:
            msg = (
                f"the write reported success but property {name!r} on page {page_id} reads back as "
                f"{landed!r}, not {intended.expected_plain!r} — treat the write as failed."
            )
            raise NotionWriteNotLandedError(msg)
        return PropertyWriteResult(
            name=name,
            type=property_type(before),
            previous=plain_property_value(before),
            value=landed,
        )


def _spans_value(prop: RawAPIDict) -> str:
    spans = prop.get(property_type(prop))
    return rich_text_plain(cast("list[RawAPIDict]", spans)) if isinstance(spans, list) else ""


def _option_value(prop: RawAPIDict) -> str:
    return option_name(prop) or ""


def _multi_select_value(prop: RawAPIDict) -> str:
    options = prop.get("multi_select")
    if not isinstance(options, list):
        return ""
    return ", ".join(str(cast("RawAPIDict", item).get("name", "")) for item in options)


def _scalar_value(prop: RawAPIDict) -> str:
    raw = prop.get(property_type(prop))
    return "" if raw is None else str(raw)


def _checkbox_value(prop: RawAPIDict) -> str:
    return "true" if prop.get("checkbox") else "false"


def _date_value(prop: RawAPIDict) -> str:
    span = prop.get("date")
    if not isinstance(span, dict):
        return ""
    typed = cast("RawAPIDict", span)
    start = str(typed.get("start") or "")
    end = str(typed.get("end") or "")
    return f"{start} → {end}" if end else start


def _unique_id_value(prop: RawAPIDict) -> str:
    payload = prop.get("unique_id")
    if not isinstance(payload, dict):
        return ""
    typed = cast("RawAPIDict", payload)
    prefix = str(typed.get("prefix") or "")
    number = str(typed.get("number", ""))
    return f"{prefix}-{number}" if prefix else number


_READERS: dict[str, Callable[[RawAPIDict], str]] = {
    "title": _spans_value,
    "rich_text": _spans_value,
    "status": _option_value,
    "select": _option_value,
    "multi_select": _multi_select_value,
    "url": _scalar_value,
    "email": _scalar_value,
    "phone_number": _scalar_value,
    "number": _scalar_value,
    "created_time": _scalar_value,
    "last_edited_time": _scalar_value,
    "checkbox": _checkbox_value,
    "date": _date_value,
    "unique_id": _unique_id_value,
}


def _spans_write(kind: str, value: str) -> PropertyWrite:
    return PropertyWrite(payload={kind: literal_rich_text(value)}, expected_plain=value)


def _option_write(kind: str, value: str) -> PropertyWrite:
    return PropertyWrite(payload={kind: {"name": value} if value else None}, expected_plain=value)


def _text_write(kind: str, value: str) -> PropertyWrite:
    return PropertyWrite(payload={kind: value or None}, expected_plain=value)


def _multi_select_write(value: str) -> PropertyWrite:
    names = [item.strip() for item in value.split(",") if item.strip()]
    return PropertyWrite(payload={"multi_select": [{"name": name} for name in names]}, expected_plain=", ".join(names))


def _number_write(value: str) -> PropertyWrite:
    if not value:
        return PropertyWrite(payload={"number": None}, expected_plain="")
    try:
        parsed = float(value)
    except ValueError as exc:
        msg = f"{value!r} is not a number, and a number property will not hold it."
        raise NotionUnwritablePropertyError(msg) from exc
    number = int(parsed) if parsed.is_integer() else parsed
    return PropertyWrite(payload={"number": number}, expected_plain=str(number))


def _checkbox_write(value: str) -> PropertyWrite:
    normalized = value.strip().lower()
    if normalized not in _TRUE | _FALSE:
        msg = f"{value!r} is neither true nor false, and a checkbox property will not hold it."
        raise NotionUnwritablePropertyError(msg)
    checked = normalized in _TRUE
    return PropertyWrite(payload={"checkbox": checked}, expected_plain="true" if checked else "false")


def _date_write(value: str) -> PropertyWrite:
    if not value:
        return PropertyWrite(payload={"date": None}, expected_plain="")
    try:
        datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"{value!r} is not an ISO 8601 date or datetime, and a date property will not hold it."
        raise NotionUnwritablePropertyError(msg) from exc
    return PropertyWrite(payload={"date": {"start": value}}, expected_plain=value)


_WRITERS: dict[str, Callable[[str], PropertyWrite]] = {
    "title": lambda value: _spans_write("title", value),
    "rich_text": lambda value: _spans_write("rich_text", value),
    "status": lambda value: _option_write("status", value),
    "select": lambda value: _option_write("select", value),
    "url": lambda value: _text_write("url", value),
    "email": lambda value: _text_write("email", value),
    "phone_number": lambda value: _text_write("phone_number", value),
    "multi_select": _multi_select_write,
    "number": _number_write,
    "checkbox": _checkbox_write,
    "date": _date_write,
}
