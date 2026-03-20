"""Tests for teetree.core.views._startup — perform_sync and _write_skill_metadata_cache."""

import json

import pytest
from django.test import override_settings

from teetree.core.sync import SyncResult


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_perform_sync_calls_sync_and_writes_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """perform_sync() calls sync_followup and _write_skill_metadata_cache."""
    fake_result = SyncResult(mrs_found=5, tickets_created=2)
    monkeypatch.setattr("teetree.core.views._startup.sync_followup", lambda: fake_result)
    monkeypatch.setattr("teetree.core.views._startup.DATA_DIR", tmp_path)

    from teetree.core.views._startup import perform_sync  # noqa: PLC0415

    result = perform_sync()

    assert result.mrs_found == 5
    assert result.tickets_created == 2

    cache_path = tmp_path / "skill-metadata.json"
    assert cache_path.exists()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_write_skill_metadata_cache_creates_parent_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """_write_skill_metadata_cache creates parent directories if missing."""
    nested = tmp_path / "deep" / "nested"
    monkeypatch.setattr("teetree.core.views._startup.DATA_DIR", nested)

    from teetree.core.views._startup import _write_skill_metadata_cache  # noqa: PLC0415

    _write_skill_metadata_cache()

    cache_path = nested / "skill-metadata.json"
    assert cache_path.exists()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_write_skill_metadata_cache_content_matches_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """Cache content matches the overlay's get_skill_metadata() output."""
    monkeypatch.setattr("teetree.core.views._startup.DATA_DIR", tmp_path)

    from teetree.core.views._startup import _write_skill_metadata_cache  # noqa: PLC0415

    _write_skill_metadata_cache()

    cache_path = tmp_path / "skill-metadata.json"
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    # CommandOverlay.get_skill_metadata() returns {} (default from OverlayBase)
    assert data == {}
