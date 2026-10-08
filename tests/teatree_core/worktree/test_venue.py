"""``observe`` answers from ``stat`` alone, so a permission error reads as out of reach on every interpreter (#4923).

``Path.is_dir()`` swallows a permission error on 3.14 and raises it on 3.13, so a live
checkout behind a directory this process cannot search read as ABSENT on one and crashed
the caller on the other.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from teatree.core.worktree.venue import VenueObservation, observe, venue_can_observe


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "here").mkdir()
    (tmp_path / "a-file").write_text("not a checkout", encoding="utf-8")
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    return tmp_path


@pytest.fixture
def unsearchable_parent(tmp_path: Path) -> Iterator[Path]:
    parent = tmp_path / "unsearchable"
    (parent / "checkout").mkdir(parents=True)
    parent.chmod(0o200)
    yield parent
    parent.chmod(0o700)


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("here", VenueObservation.PRESENT),
        ("gone", VenueObservation.ABSENT),
        ("a-file", VenueObservation.ABSENT),
        ("dangling", VenueObservation.ABSENT),
        ("never-mounted/gone", VenueObservation.UNOBSERVABLE),
    ],
)
def test_observe_tells_present_from_absent_from_never_mounted(tree: Path, relative: str, expected: object) -> None:
    assert observe(tree / relative) is expected


def test_a_checkout_behind_a_parent_this_process_cannot_search_is_unobservable(unsearchable_parent: Path) -> None:
    assert observe(unsearchable_parent / "checkout") is VenueObservation.UNOBSERVABLE
    assert observe(unsearchable_parent / "never-existed") is VenueObservation.UNOBSERVABLE


def test_a_parent_that_is_searchable_but_not_listable_is_authoritative(tmp_path: Path) -> None:
    parent = tmp_path / "unlistable"
    (parent / "checkout").mkdir(parents=True)
    parent.chmod(0o300)
    try:
        assert observe(parent / "checkout") is VenueObservation.PRESENT
        assert observe(parent / "gone") is VenueObservation.ABSENT
    finally:
        parent.chmod(0o700)


def test_a_reaper_may_not_call_a_checkout_dead_that_it_cannot_search_for(unsearchable_parent: Path) -> None:
    assert venue_can_observe(unsearchable_parent / "checkout", (unsearchable_parent.parent,)) is False
