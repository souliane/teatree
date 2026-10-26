from datetime import UTC, datetime, timedelta

from teatree.utils.secret_backups import backup_name, expired_backup_names


def test_expiry_applies_count_and_age_per_base() -> None:
    now = datetime(2026, 10, 25, tzinfo=UTC)
    first = [backup_name("team/a", now - timedelta(days=age)) for age in (1, 2, 3, 4)]
    second = [backup_name("team/b", now - timedelta(days=age)) for age in (1, 2, 91)]
    assert expired_backup_names(first + second, now) == frozenset({first[-1], second[-1]})
