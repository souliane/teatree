"""Cold-safe field declarations shared by settings resolution and schema rendering."""

import dataclasses
from collections.abc import Callable, Iterable
from dataclasses import MISSING, field
from enum import StrEnum, auto
from typing import Any, cast, get_type_hints

from teatree.config.mr_reminder import MrReminderConfig
from teatree.types import SpeakConfig

_UNSET = object()


class Category(StrEnum):
    DEFAULT = auto()
    PERSONAL = auto()
    SECRET = auto()


class Registry(StrEnum):
    OVERLAY = auto()
    COLD = auto()
    COLD_HOOK = auto()
    REGISTRY = auto()


@dataclasses.dataclass(frozen=True)
class SettingMeta:
    category: Category
    registry: Registry
    box_global: bool = False


@dataclasses.dataclass(frozen=True)
class Bounds:
    minimum: float
    maximum: float | None = None
    zero_disables: bool = False
    why: str = ""


@dataclasses.dataclass(frozen=True)
class DeclaredSetting:
    annotation: object
    default: object
    help: str
    parse: Callable[[Any], Any]
    category: Category
    box_global: bool
    stored: object
    storage_parse: Callable[[Any], Any]
    storage_default: object
    bounds: Bounds | None


@dataclasses.dataclass(frozen=True)
class SettingSpec:
    help: str
    parse: Callable[[Any], Any]
    category: Category = Category.DEFAULT
    box_global: bool = False
    stored: object = _UNSET
    storage_parse: Callable[[Any], Any] | None = None
    storage_default: object = _UNSET
    bounds: Bounds | None = None


def stored_form(value: object) -> object:
    return value.to_dict() if isinstance(value, SpeakConfig | MrReminderConfig) else value


def setting[T](
    default: T | object = MISSING,
    *,
    default_factory: Callable[[], T] | object = MISSING,
    spec: SettingSpec,
) -> T:
    metadata = {"setting": spec}
    if default_factory is not MISSING:
        return field(default_factory=cast("Callable[[], T]", default_factory), metadata=metadata)
    if default is not MISSING:
        return field(default=cast("T", default), metadata=metadata)
    return cast("T", field(metadata=metadata))


def declared_fields(settings_type: type[Any]) -> dict[str, DeclaredSetting]:
    hints = get_type_hints(settings_type)
    instance = settings_type()
    rows: dict[str, DeclaredSetting] = {}
    for declared in dataclasses.fields(settings_type):
        spec: SettingSpec = declared.metadata["setting"]
        default = getattr(instance, declared.name)
        rows[declared.name] = DeclaredSetting(
            annotation=hints[declared.name],
            default=default,
            help=spec.help,
            parse=spec.parse,
            category=spec.category,
            box_global=spec.box_global,
            stored=hints[declared.name] if spec.stored is _UNSET else spec.stored,
            storage_parse=spec.parse if spec.storage_parse is None else spec.storage_parse,
            storage_default=stored_form(default) if spec.storage_default is _UNSET else spec.storage_default,
            bounds=spec.bounds,
        )
    return rows


def render_declared_keys(settings_type: type[Any]) -> str:
    return render_keys_manifest("", declared_fields(settings_type))


def render_keys_manifest(existing: str, keys: Iterable[str]) -> str:
    active: set[str] = set()
    retired: set[str] = set()
    for line in existing.splitlines():
        if line.startswith("RETIRED "):
            retired.add(line.removeprefix("RETIRED "))
        elif line.startswith("RENAMED "):
            retired.add(line.split(" ", 2)[1])
        elif line and not line.startswith("#"):
            active.add(line)
    current = set(keys)
    vanished = active - current
    if vanished:
        msg = f"declared keys vanished without RENAMED/RETIRED entries: {sorted(vanished)}"
        raise ValueError(msg)
    revived = current & retired
    if revived:
        msg = f"retired keys were reintroduced: {sorted(revived)}"
        raise ValueError(msg)
    additions = "".join(f"{key}\n" for key in sorted(current - active))
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    return existing + prefix + additions


def validate_manifest_history(committed: str, candidate: str) -> None:
    candidate_lines = set(candidate.splitlines())
    for line in committed.splitlines():
        if not line or line.startswith("#") or line in candidate_lines:
            continue
        if line.startswith(("RETIRED ", "RENAMED ")):
            msg = f"historical disposition was removed: {line}"
            raise ValueError(msg)
        if f"RETIRED {line}" in candidate_lines or any(row.startswith(f"RENAMED {line} ") for row in candidate_lines):
            continue
        msg = f"declared key was removed without a disposition: {line}"
        raise ValueError(msg)
