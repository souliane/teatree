"""Read-only settings change group for check-in reports."""

from datetime import datetime

from django.db.models import Q, QuerySet

from teatree.core.checking_types import CheckGroup, CheckItem
from teatree.core.models.config_setting_change import ConfigSettingChange


def _group(rows: QuerySet[ConfigSettingChange], *, cap: int) -> CheckGroup:
    total = rows.count()
    items = [
        CheckItem(
            label=row.key,
            url="",
            detail=f"{row.scope or 'global'} · {row.written_by or 'unknown'} · {row.old_value} → {row.new_value}",
        )
        for row in rows.order_by("-changed_at", "-pk")[:cap]
    ]
    return CheckGroup(title="Settings", items=items, total=total)


def gather_settings_group(*, since: datetime, now: datetime, overlay_name: str, cap: int) -> CheckGroup:
    scopes = ["", overlay_name] if overlay_name else [""]
    rows = ConfigSettingChange.objects.filter(changed_at__gte=since, changed_at__lt=now, scope__in=scopes)
    return _group(rows, cap=cap)


def gather_all_settings_group(*, overlay_windows: dict[str, tuple[datetime, datetime]], cap: int) -> CheckGroup:
    if not overlay_windows:
        return CheckGroup(title="Settings")
    windows = Q(pk__in=[])
    for scope, (since, now) in overlay_windows.items():
        windows |= Q(scope="", changed_at__gte=since, changed_at__lt=now)
        windows |= Q(scope=scope, changed_at__gte=since, changed_at__lt=now)
    return _group(ConfigSettingChange.objects.filter(windows), cap=cap)
