"""Turning live config objects into the snapshot's JSON, and hashing it stably.

Every hash in a snapshot is taken over :func:`canonical_json`, so two boxes that hold the
same value produce the same digest whatever order their dicts were built in.

A withheld value never reaches the payload: :func:`redaction_stub` replaces it with WHY it
was withheld plus a digest of the real value, which is what lets two boxes be compared for
difference without either one carrying the value.
"""

import datetime as dt
import hashlib
import json
import os
from collections.abc import Mapping
from enum import Enum
from typing import Final

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None

#: teatree keys every seed row by ``name`` (``live_seed_rows``), so that attribute is the row identity.
ROW_KEY_ATTR: Final = "name"


def serialise(value: object) -> Json:
    """*value* as JSON, by the format's rules: times as text, enums as their value, Paths as str."""
    if value is None:
        return None
    if isinstance(value, Enum):
        return serialise(value.value)
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dt.time):
        return _time_text(value)
    if isinstance(value, os.PathLike):
        return str(value)
    return _serialise_container(value)


def canonical_json(value: object) -> str:
    """*value* as the one byte-stable JSON text every hash in the snapshot is taken over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(value: object) -> str:
    """The digest of *value*'s canonical JSON — the one shape every fingerprint field takes."""
    return sha256_of(canonical_json(value))


def redaction_stub(reason: str, value: object) -> dict[str, str]:
    """A withheld value: why it was withheld, plus a hash so two boxes can still be compared."""
    return {"__redacted__": reason, "sha256": digest(serialise(value))}


def field_names(model: object, *, relations: bool) -> list[str]:
    """*model*'s own configuration fields — no primary key, no reverse relation, no timestamp."""
    meta = getattr(model, "_meta", None)
    get_fields = getattr(meta, "get_fields", None)
    if not callable(get_fields):
        return []
    names = []
    for model_field in get_fields():
        related = bool(getattr(model_field, "is_relation", False))
        if getattr(model_field, "primary_key", False) or (related and getattr(model_field, "auto_created", False)):
            continue
        if (related and not relations) or _is_timestamp(model_field):
            continue
        names.append(str(model_field.name))
    return names


def _time_text(value: dt.time) -> str:
    return value.strftime("%H:%M" if not value.second and not value.microsecond else "%H:%M:%S")


def _serialise_container(value: object) -> Json:
    if isinstance(value, Mapping):
        return {str(key): serialise(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        return sorted((serialise(item) for item in value), key=canonical_json)
    if isinstance(value, list | tuple):
        return [serialise(item) for item in value]
    rows = getattr(value, "all", None)
    if callable(rows):  # a Django related manager / queryset
        return [serialise(row) for row in rows()]
    if hasattr(value, "_meta"):
        return _model_row(value)
    return str(value)


def _model_row(row: object) -> Json:
    """A model instance as its name when it has one, else a dict of its own plain fields."""
    name = getattr(row, ROW_KEY_ATTR, None)
    if isinstance(name, str) and name:
        return name
    return {field: serialise(getattr(row, field, None)) for field in field_names(type(row), relations=False)}


def _is_timestamp(model_field: object) -> bool:
    # a DateTimeField on a seed row is a runtime stamp; the shipped seed tables carry none
    internal = getattr(model_field, "get_internal_type", None)
    return callable(internal) and internal() == "DateTimeField"


__all__ = [
    "ROW_KEY_ATTR",
    "Json",
    "canonical_json",
    "digest",
    "field_names",
    "redaction_stub",
    "serialise",
    "sha256_of",
]
