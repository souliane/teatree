"""Append-only history for persisted configuration changes."""

from typing import ClassVar

from django.db import models

from teatree.config.value_visibility import MASKED, withholds_value


class ConfigSettingChange(models.Model):
    scope = models.CharField(max_length=255, default="", blank=True)
    key = models.CharField(max_length=255)
    action = models.CharField(max_length=10)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    written_by = models.CharField(max_length=255)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "teatree_config_setting_change"
        indexes: ClassVar = [models.Index(fields=["key", "scope", "changed_at"], name="config_change_lookup")]

    def __str__(self) -> str:
        return f"config-setting-change<{self.scope or 'global'} {self.key} {self.action}>"

    @classmethod
    def record(
        cls, *, key: str, scope: str, action: str, values: tuple[object, object], written_by: str
    ) -> "ConfigSettingChange":
        old_value, new_value = values
        return cls.objects.create(
            key=key,
            scope=scope,
            action=action,
            old_value=MASKED if old_value is not None and withholds_value(key, old_value) else old_value,
            new_value=MASKED if new_value is not None and withholds_value(key, new_value) else new_value,
            written_by=written_by,
        )
