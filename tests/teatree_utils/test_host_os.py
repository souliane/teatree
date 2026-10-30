from pathlib import Path

from teatree.utils.host_os import running_in_a_container


def test_a_container_marker_makes_the_process_a_container(tmp_path: Path) -> None:
    marker = tmp_path / "container"
    marker.touch()
    assert running_in_a_container((marker,))
    assert not running_in_a_container((tmp_path / "absent",))
