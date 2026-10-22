"""Pydantic view of the cold-safe setting declarations."""

from collections.abc import Callable
from enum import StrEnum
from functools import lru_cache
from operator import getitem
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, Union, cast, get_args, get_origin

from pydantic import BeforeValidator, create_model
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from teatree.config.declaration import Category, Registry, SettingMeta, declared_fields
from teatree.config.registries import COLD_HOOK_SETTINGS, COLD_SETTINGS, REGISTRY_SETTINGS, ColdHookSetting
from teatree.config.settings import UserSettings

__all__ = [
    "Category",
    "Registry",
    "SettingMeta",
    "TeatreeSettingsSchema",
    "derive_box_global_settings",
    "derive_cold_entries",
    "derive_global_only_settings",
    "derive_overlay_overridable_settings",
    "setting_choices",
    "setting_meta",
    "shipped_defaults",
]


def _annotated(annotation: object, parser: Callable[[Any], Any], meta: SettingMeta) -> object:
    return getitem(Annotated, (annotation, BeforeValidator(parser), meta))


def schema_fields_for(settings_type: type[Any]) -> dict[str, tuple[object, object]]:
    return {
        key: (
            _annotated(row.stored, row.storage_parse, SettingMeta(row.category, Registry.OVERLAY, row.box_global)),
            row.storage_default,
        )
        for key, row in declared_fields(settings_type).items()
    }


def _cold_schema_fields() -> dict[str, tuple[object, object]]:
    fields: dict[str, tuple[object, object]] = {}
    for registry, entries in (
        (Registry.COLD, COLD_SETTINGS),
        (Registry.COLD_HOOK, COLD_HOOK_SETTINGS),
        (Registry.REGISTRY, REGISTRY_SETTINGS),
    ):
        for key, entry in entries.items():
            if entry.stored is None:
                msg = f"{key} has no storage annotation"
                raise TypeError(msg)
            fields[key] = (
                _annotated(entry.stored, entry.parse, SettingMeta(entry.category, registry)),
                entry.default,
            )
    return fields


class _SchemaBase(BaseSettings):
    model_config = SettingsConfigDict(extra="forbid", validate_default=True, frozen=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        _ = (settings_cls, env_settings, dotenv_settings, file_secret_settings)
        return (init_settings,)


TeatreeSettingsSchema = create_model(
    "TeatreeSettingsSchema",
    __base__=_SchemaBase,
    __module__=__name__,
    **cast("dict[str, Any]", dict(sorted({**schema_fields_for(UserSettings), **_cold_schema_fields()}.items()))),
)


def setting_meta(key: str) -> SettingMeta:
    return next(m for m in TeatreeSettingsSchema.model_fields[key].metadata if isinstance(m, SettingMeta))


def _enumerated(annotation: object) -> tuple[object, ...]:
    if annotation is bool:
        return (True, False)
    if get_origin(annotation) is Literal:
        return get_args(annotation)
    if get_origin(annotation) in {Union, UnionType}:
        return tuple(v for arg in get_args(annotation) for v in ((None,) if arg is NoneType else _enumerated(arg)))
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return tuple(member.value for member in annotation)
    return ()


def setting_choices(key: str) -> tuple[object, ...]:
    field = TeatreeSettingsSchema.model_fields.get(key)
    return () if field is None else _enumerated(field.annotation)


@lru_cache(maxsize=1)
def shipped_defaults() -> BaseSettings:
    return TeatreeSettingsSchema()


def _before_validator(key: str) -> Callable[[Any], Any]:
    return next(m.func for m in TeatreeSettingsSchema.model_fields[key].metadata if isinstance(m, BeforeValidator))


def _keys_in(registry: Registry) -> list[str]:
    return [key for key in TeatreeSettingsSchema.model_fields if setting_meta(key).registry is registry]


def derive_overlay_overridable_settings() -> dict[str, Callable[[Any], Any]]:
    return {key: row.parse for key, row in declared_fields(UserSettings).items()}


def derive_box_global_settings() -> frozenset[str]:
    return frozenset(key for key in TeatreeSettingsSchema.model_fields if setting_meta(key).box_global)


def derive_global_only_settings() -> frozenset[str]:
    return frozenset(
        key for key in TeatreeSettingsSchema.model_fields if setting_meta(key).registry is not Registry.OVERLAY
    )


def derive_cold_entries(registry: Registry) -> dict[str, ColdHookSetting]:
    defaults = shipped_defaults()
    return {key: ColdHookSetting(_before_validator(key), default=getattr(defaults, key)) for key in _keys_in(registry)}
