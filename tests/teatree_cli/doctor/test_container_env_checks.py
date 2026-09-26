"""Checks over the deployed stack's create-time GPG environment."""

import inspect
import io
from contextlib import redirect_stdout

from teatree.cli.doctor import run_checks
from teatree.cli.doctor.checks_container_env import HOST_GNUPG_MOUNT, check_deployed_gnupg_home

CONTAINER_LOCAL_HOME = "/home/teatree/.gnupg-run/gnupg"


def _healthy(**extra: str) -> dict[str, str]:
    return {"GNUPGHOME": CONTAINER_LOCAL_HOME, "TEATREE_HOST_GNUPG_DIR": HOST_GNUPG_MOUNT, **extra}


def _run_gnupg_home_check(containers: dict[str, dict[str, str]] | None) -> tuple[bool, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        ok = check_deployed_gnupg_home(probe=lambda: containers)
    return ok, out.getvalue()


class TestTheDeployedContainerEnvironmentIsTheEvidence:
    def test_passes_when_every_running_container_pins_the_container_local_home(self) -> None:
        ok, msg = _run_gnupg_home_check({"teatree-worker": _healthy(), "teatree-admin": _healthy()})
        assert ok is True
        assert msg == ""

    def test_fails_when_a_container_still_starts_from_the_host_keybox(self) -> None:
        # The measured five-day window: image built 08:38Z, fix merged 11:50Z, every
        # `docker exec` inheriting the host mount from an image nothing re-converged.
        stale = {"GNUPGHOME": HOST_GNUPG_MOUNT, "TEATREE_HOST_GNUPG_DIR": HOST_GNUPG_MOUNT}
        ok, msg = _run_gnupg_home_check({"teatree-worker": stale, "teatree-admin": _healthy()})
        assert ok is False
        assert "teatree-worker" in msg
        assert "teatree-admin" not in msg
        assert HOST_GNUPG_MOUNT in msg

    def test_fails_when_a_container_carries_no_gnupg_home_at_all(self) -> None:
        # gpg then defaults to $HOME/.gnupg, which IS the host bind mount.
        ok, msg = _run_gnupg_home_check({"teatree-worker": {"TEATREE_HOST_GNUPG_DIR": HOST_GNUPG_MOUNT}})
        assert ok is False
        assert "teatree-worker" in msg

    def test_fails_when_the_home_is_a_subdirectory_of_the_host_mount(self) -> None:
        # Still on the bind mount, so the dotlock is still the host's to be blocked by.
        inside = {"GNUPGHOME": f"{HOST_GNUPG_MOUNT}/inner", "TEATREE_HOST_GNUPG_DIR": HOST_GNUPG_MOUNT}
        ok, msg = _run_gnupg_home_check({"teatree-worker": inside})
        assert ok is False
        assert f"{HOST_GNUPG_MOUNT}/inner" in msg

    def test_judges_against_the_host_dir_the_container_itself_names(self) -> None:
        # The verdict is computed from the deployed artifact alone — never from a path
        # this checkout's source happens to spell.
        relocated = {"GNUPGHOME": "/srv/keys", "TEATREE_HOST_GNUPG_DIR": "/srv/keys"}
        ok, msg = _run_gnupg_home_check({"teatree-worker": relocated})
        assert ok is False
        assert "/srv/keys" in msg

    def test_passes_when_the_named_host_dir_is_a_different_directory(self) -> None:
        ok, msg = _run_gnupg_home_check({"teatree-worker": {"GNUPGHOME": "/srv/keys", "TEATREE_HOST_GNUPG_DIR": "/h"}})
        assert ok is True
        assert msg == ""

    def test_is_silent_when_docker_cannot_answer(self) -> None:
        ok, msg = _run_gnupg_home_check(None)
        assert ok is True
        assert msg == ""

    def test_is_silent_when_no_stack_container_is_running(self) -> None:
        ok, msg = _run_gnupg_home_check({})
        assert ok is True
        assert msg == ""


def test_the_recurring_container_doctor_never_invokes_the_host_lock_sentinel() -> None:
    assert "check_host_keybox_lock" not in inspect.getsource(run_checks.run_doctor_checks)
