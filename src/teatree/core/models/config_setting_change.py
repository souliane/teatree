"""Append-only, mask-safe history of configuration changes."""

from typing import ClassVar

from django.db import models

from teatree.config.display import masked_display


class ConfigSettingChange(models.Model):
    scope = models.CharField(max_length=255, default="", blank=True)
    key = models.CharField(max_length=255)
    action = models.CharField(max_length=16)
    old_value = models.TextField()
    new_value = models.TextField()
    written_by = models.CharField(max_length=255, default="", blank=True)
    changed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "teatree_config_setting_change"
        ordering: ClassVar = ["-changed_at", "-pk"]

    def __str__(self) -> str:
        return f"config-setting-change<{self.scope or 'global'} {self.key} {self.action}>"

    @classmethod
    def record(
        cls, *, scope: str, key: str, action: str, values: tuple[object, object], written_by: str
    ) -> "ConfigSettingChange":
        old_value, new_value = values
        return cls.objects.create(
            scope=scope,
            key=key,
            action=action,
            old_value=masked_display(key, old_value),
            new_value=masked_display(key, new_value),
            written_by=written_by,
        )
