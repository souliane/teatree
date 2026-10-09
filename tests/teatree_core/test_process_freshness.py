"""A snapshot that froze no head is UNKNOWN, never a green CURRENT record (#4411).

``_compare()`` seeds its accumulator with a bare ``CURRENT`` reading, so a snapshot whose
frozen half is empty — every app's ``max_migration.txt`` unreadable — fell straight through
to it. The one state where the process knows *nothing* about its own freshness was recorded
as the healthiest answer, with empty heads indistinguishable afterwards from a measured
match, and no warning anywhere.

Admission is unchanged in both directions: UNKNOWN admitted before this and admits now — the
fail-open direction is right here, and only the silence and the green label were wrong.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from django.apps import apps

from teatree.core.process_freshness import (
    FreshnessVerdict,
    cached_process_freshness,
    code_behind_schema,
    invalidate_process_freshness,
    published_readings,
    read_process_freshness,
    record_loaded_snapshot,
    reset_loaded_snapshot,
)
from teatree.utils.throttled_log import reset_throttle

_LOGGER = "teatree.core.process_freshness"


@dataclass(frozen=True)
class _AppWithNoHeadFile:
    """An app config whose migrations dir does not exist, so every head read raises ``OSError``."""

    label: str
    path: str


@pytest.fixture
def snapshot_that_froze_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TEATREE_ROLE", "worker")
    reset_throttle()
    invalidate_process_freshness()
    reset_loaded_snapshot()
    unreadable = [_AppWithNoHeadFile(label="core", path=str(tmp_path / "never-provisioned"))]
    with patch.object(apps, "get_app_configs", return_value=unreadable):
        record_loaded_snapshot()
    yield
    # Process-wide state every later case in this xdist worker reads, so re-freeze the real one.
    reset_loaded_snapshot()
    record_loaded_snapshot()
    invalidate_process_freshness()
    reset_throttle()


def test_no_readable_head_is_unknown_rather_than_current(snapshot_that_froze_nothing: None) -> None:
    reading = read_process_freshness()

    assert reading.verdict is FreshnessVerdict.UNKNOWN
    assert "max_migration.txt" in reading.detail
    assert reading.block_reason() == ""


def test_no_readable_head_warns_instead_of_passing_quietly(
    snapshot_that_froze_nothing: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        read_process_freshness()

    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert warnings, "the total-measurement-failure branch must be visible, like every other fail-open branch"
    assert "max_migration.txt" in warnings[0].getMessage()


def test_the_published_record_carries_unknown_not_a_green_current(snapshot_that_froze_nothing: None) -> None:
    cached_process_freshness()

    assert [reading.verdict for reading in published_readings()] == [str(FreshnessVerdict.UNKNOWN)]


def test_admission_is_unchanged_because_unknown_still_admits(snapshot_that_froze_nothing: None) -> None:
    assert code_behind_schema() == ""
