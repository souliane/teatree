"""``physical_location`` — one token per physical directory, whatever namespace spells the path (#4923)."""

from pathlib import Path

from teatree.mount_identity import physical_location

_CHECKOUT = Path("/nonexistent-t3/mnt/ws/ticket/repo")
_BOOT = "11111111-2222-3333-4444-555555555555\n"

_HOST = "22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n"
_CONTAINER_OVERLAY_ROOT = "1108 497 0:35 / / rw,relatime - overlay overlay rw\n"
_CONTAINER_IDENTITY_BIND = (
    _CONTAINER_OVERLAY_ROOT + "1200 1108 259:2 /nonexistent-t3/mnt/ws /nonexistent-t3/mnt/ws rw - ext4 /dev/x rw\n"
)
_CONTAINER_VOLUME = (
    _CONTAINER_OVERLAY_ROOT
    + "1201 1108 259:2 /var/lib/docker/volumes/ws/_data /nonexistent-t3/mnt/ws rw - ext4 /dev/x rw\n"
)


def _locate(table: str, boot: str = _BOOT) -> str | None:
    return physical_location(_CHECKOUT, mountinfo=table, boot_id=boot)


def test_an_identity_bind_reaches_the_same_directory_the_host_does() -> None:
    assert _locate(_CONTAINER_IDENTITY_BIND) == _locate(_HOST) == "259:2:/nonexistent-t3/mnt/ws/ticket/repo"


def test_a_container_private_path_never_matches_the_host() -> None:
    assert _locate(_CONTAINER_OVERLAY_ROOT) != _locate(_HOST)


def test_a_volume_mounted_at_the_same_path_is_a_different_directory() -> None:
    assert _locate(_CONTAINER_VOLUME) == "259:2:/var/lib/docker/volumes/ws/_data/ticket/repo"
    assert _locate(_CONTAINER_VOLUME) != _locate(_HOST)


def test_an_anonymous_device_is_scoped_to_the_boot() -> None:
    first = _locate(_CONTAINER_OVERLAY_ROOT)
    assert first == f"0:35@{_BOOT.strip()}:{_CHECKOUT}"
    assert _locate(_CONTAINER_OVERLAY_ROOT, boot="another-boot") != first
    assert _locate(_CONTAINER_OVERLAY_ROOT, boot="") is None


def test_the_last_of_two_stacked_mounts_is_the_one_on_top() -> None:
    stacked = _HOST + "30 22 259:9 / /nonexistent-t3/mnt/ws rw - ext4 /dev/y rw\n"
    stacked += "31 30 259:7 /sub /nonexistent-t3/mnt/ws rw - ext4 /dev/z rw\n"

    assert _locate(stacked) == "259:7:/sub/ticket/repo"


def test_an_escaped_mount_point_is_read_as_the_path_it_names() -> None:
    table = _HOST + r"40 22 259:3 / /nonexistent-t3/my\040home rw - ext4 /dev/w rw" + "\n"

    assert physical_location(Path("/nonexistent-t3/my home/x"), mountinfo=table) == "259:3:/x"


def test_an_unreadable_mount_table_has_no_location(tmp_path: Path) -> None:
    assert physical_location(tmp_path, mountinfo="") is None
    assert physical_location(tmp_path, mountinfo="malformed\n") is None


def test_this_process_can_locate_a_real_directory(tmp_path: Path) -> None:
    location = physical_location(tmp_path)
    child = physical_location(tmp_path / "absent" / "child")

    assert location is not None
    assert child == f"{location.rstrip('/')}/absent/child"
