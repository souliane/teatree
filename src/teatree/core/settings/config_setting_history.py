"""Audit-backed recovery values for the settings command."""

from dataclasses import dataclass

from django.db import transaction

from teatree.config.value_visibility import MASKED, withholds_value
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.config_setting_change import ConfigSettingChange


@dataclass(frozen=True, slots=True)
class ClearHistory:
    removed: str
    previous: str


def latest_change(key: str, scope: str) -> ConfigSettingChange | None:
    return ConfigSettingChange.objects.filter(key=key, scope=scope).order_by("-pk").first()


def _value_text(value: object | None) -> str:
    return "none" if value is None else repr(value)


def previous_value_after_set(key: str, scope: str, before_pk: int | None, current: object) -> str:
    latest = latest_change(key, scope)
    if latest is not None and latest.pk != before_pk:
        return _value_text(latest.old_value)
    return repr(MASKED if withholds_value(key, current) else current)


def clear_with_history(key: str, scope: str) -> ClearHistory | None:
    with transaction.atomic(using=ConfigSetting.objects.db):
        before = latest_change(key, scope)
        if not ConfigSetting.objects.clear(key, scope=scope):
            return None
        after = latest_change(key, scope)
        removed = _value_text(after.old_value) if after else "unknown"
        previous = (
            _value_text(before.old_value)
            if before is not None and after is not None and before.new_value == after.old_value
            else "unknown"
        )
    return ClearHistory(removed, previous)
