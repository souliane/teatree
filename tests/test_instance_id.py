"""Tests for ``teatree.instance_id`` — the durable per-installation identity."""

import os
import uuid
from pathlib import Path

import pytest

from teatree.instance_id import machine_data_dir, read_or_create_instance_id


def test_creates_a_valid_uuid_on_first_read(tmp_path: Path) -> None:
    value = read_or_create_instance_id(tmp_path)
    uuid.UUID(value)  # raises if not a well-formed UUID
    assert (tmp_path / "instance_id").read_text(encoding="utf-8").strip() == value


def test_stable_across_process_restarts(tmp_path: Path) -> None:
    # Two independent calls model two process starts against the same data dir;
    # the persisted file makes the second read return the first's value verbatim.
    first = read_or_create_instance_id(tmp_path)
    second = read_or_create_instance_id(tmp_path)
    assert first == second


def test_distinct_per_machine(tmp_path: Path) -> None:
    # Two machines resolve to two different data dirs → two different ids.
    machine_a = read_or_create_instance_id(tmp_path / "machine-a")
    machine_b = read_or_create_instance_id(tmp_path / "machine-b")
    assert machine_a != machine_b


def test_malformed_file_is_replaced_not_trusted(tmp_path: Path) -> None:
    (tmp_path / "instance_id").write_text("not-a-uuid", encoding="utf-8")
    value = read_or_create_instance_id(tmp_path)
    uuid.UUID(value)


def _plant_then_link(winner: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``os.link`` model a racing writer: the winner's file lands first, so our link fails."""
    real_link = os.link

    def racing_link(src: str, dst: str) -> None:
        Path(dst).write_text(winner, encoding="utf-8")
        real_link(src, dst)  # dst now exists → FileExistsError, which the code suppresses

    monkeypatch.setattr(os, "link", racing_link)


def test_concurrent_create_reads_the_winners_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    winner = str(uuid.uuid4())
    _plant_then_link(winner, monkeypatch)
    assert read_or_create_instance_id(tmp_path) == winner


def test_concurrent_create_with_corrupt_winner_file_falls_back_to_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_then_link("garbage-not-a-uuid", monkeypatch)
    value = read_or_create_instance_id(tmp_path)
    uuid.UUID(value)  # a corrupt winner never yields an empty id


def test_machine_data_dir_ignores_worktree_isolation(tmp_path: Path) -> None:
    # A worktree checkout and the main clone must resolve the SAME machine dir,
    # so the instance id is shared across every process on the machine. The
    # dir is derived from XDG/home alone, never the per-worktree isolation root.
    env = {"XDG_DATA_HOME": str(tmp_path / "xdg")}
    resolved = machine_data_dir(env=env, home=tmp_path / "home")
    assert resolved == tmp_path / "xdg" / "teatree"

    no_xdg = machine_data_dir(env={}, home=tmp_path / "home")
    assert no_xdg == tmp_path / "home" / ".local" / "share" / "teatree"
