"""Can THIS venue drive the docker daemon? The probe behind reclaim-disk's refusal (#4585).

The measured venue split this pins: the teatree stack mounts
``/var/run/docker.sock`` into every service, but only ``teatree-worker``
declares the matching ``group_add``. So in ``teatree-admin`` the socket node is
present and unopenable — a shape no "does the file exist?" check can tell from a
working one. The probe therefore asks the daemon, and the three answers it must
keep apart are: it answered (reachable), it refused (a venue that cannot act),
and there is no docker CLI here at all.
"""

from subprocess import CompletedProcess

from teatree.docker import venue
from teatree.utils.run import TimeoutExpired


def _answers(stdout: str = "29.6.2\n"):
    def run(cmd, **_):
        return CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

    return run


def test_reachable_when_the_daemon_answers(monkeypatch):
    monkeypatch.setattr(venue, "run_allowed_to_fail", _answers())

    verdict = venue.docker_venue()

    assert verdict.reachable is True
    assert verdict.reason == ""


def test_probe_is_read_only_and_never_a_prune(monkeypatch):
    """The probe must not be able to remove anything — it asks the daemon its version."""
    calls: list[list[str]] = []

    def record(cmd, **_):
        calls.append(list(cmd))
        return CompletedProcess(args=cmd, returncode=0, stdout="29.6.2\n", stderr="")

    monkeypatch.setattr(venue, "run_allowed_to_fail", record)
    venue.docker_venue()

    assert len(calls) == 1
    assert "prune" not in calls[0]
    assert "rm" not in calls[0]
    assert calls[0][:2] == ["docker", "version"]


def test_socket_present_but_unopenable_is_refused_not_reachable(monkeypatch):
    """The admin container's exact shape: the socket node exists, the group grant does not."""
    denied = "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock"

    def refuse(cmd, **_):
        return CompletedProcess(args=cmd, returncode=1, stdout="", stderr=denied)

    monkeypatch.setattr(venue, "run_allowed_to_fail", refuse)

    verdict = venue.docker_venue()

    assert verdict.reachable is False
    assert denied in verdict.reason
    assert verdict.has_cli is True


def test_absent_cli_is_reported_as_absent_not_as_a_daemon_error(monkeypatch):
    def missing(cmd, **_):
        msg = "docker"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(venue, "run_allowed_to_fail", missing)

    verdict = venue.docker_venue()

    assert verdict.reachable is False
    assert verdict.has_cli is False
    assert "docker" in verdict.reason


def test_unopenable_socket_raising_permission_error_is_still_a_refusal(monkeypatch):
    """A ``PermissionError`` on exec is the daemon-unreachable case, never a clean venue."""

    def denied(cmd, **_):
        msg = "docker"
        raise PermissionError(msg)

    monkeypatch.setattr(venue, "run_allowed_to_fail", denied)

    verdict = venue.docker_venue()

    assert verdict.reachable is False


def test_timeout_is_unreachable_rather_than_assumed_healthy(monkeypatch):
    def hang(cmd, **_):
        raise TimeoutExpired(cmd, 1)

    monkeypatch.setattr(venue, "run_allowed_to_fail", hang)

    verdict = venue.docker_venue()

    assert verdict.reachable is False
    assert "timed out" in verdict.reason


def test_nonzero_exit_with_empty_stderr_still_names_a_reason(monkeypatch):
    def silent(cmd, **_):
        return CompletedProcess(args=cmd, returncode=2, stdout="", stderr="  ")

    monkeypatch.setattr(venue, "run_allowed_to_fail", silent)

    assert venue.docker_venue().reason == "exit status 2"


def test_service_role_is_read_from_the_container_not_guessed(monkeypatch, tmp_path):
    monkeypatch.setattr(venue, "run_allowed_to_fail", _answers())
    monkeypatch.setenv("TEATREE_ROLE", "admin")
    monkeypatch.setattr(venue, "_CONTAINER_MARKER", tmp_path / "dockerenv")
    (tmp_path / "dockerenv").touch()

    verdict = venue.docker_venue()

    assert verdict.service_role == "admin"
    assert verdict.containerized is True
    assert "admin" in verdict.description


def test_a_host_venue_claims_no_service_role(monkeypatch, tmp_path):
    monkeypatch.setattr(venue, "run_allowed_to_fail", _answers())
    monkeypatch.setenv("TEATREE_ROLE", "admin")
    monkeypatch.setattr(venue, "_CONTAINER_MARKER", tmp_path / "absent")

    verdict = venue.docker_venue()

    assert verdict.containerized is False
    assert verdict.service_role == ""


def test_probe_never_raises_on_an_unexpected_os_error(monkeypatch):
    """The pressure loop consults this on a full disk — it may never become the crash."""

    def boom(cmd, **_):
        msg = "no space left on device"
        raise OSError(msg)

    monkeypatch.setattr(venue, "run_allowed_to_fail", boom)

    verdict = venue.docker_venue()

    assert verdict.reachable is False
    assert "no space left on device" in verdict.reason


def test_description_distinguishes_a_role_a_bare_container_and_a_host():
    """The description is what a refusal names, so each venue must read differently."""
    assert venue.DockerVenue(reachable=False, containerized=True, service_role="admin").description == (
        "the admin container"
    )
    assert venue.DockerVenue(reachable=False, containerized=True).description == "this container"
    assert venue.DockerVenue(reachable=False).description == "this host"
