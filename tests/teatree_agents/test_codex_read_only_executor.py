"""A write-barring phase reaches the managed lane only through an executor a live probe found read-only."""

import copy
import dataclasses
import os
from collections.abc import Callable, Iterable, Iterator, MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch

import pytest

from teatree.agents import codex_read_only_executor, harness_registry
from teatree.agents.codex_read_only_executor import (
    EXECUTOR_PHASES,
    EXECUTOR_SERVICE,
    ExecutorAttestation,
    ExecutorRefusalError,
    attest,
    read_only_executor_refusal,
)
from teatree.agents.codex_read_only_verdict import (
    CLONE_ROOT,
    CODEX_HOME_ROOT,
    CODEX_SOURCE_GIT,
    EXECUTOR_HOME_ROOT,
    WORKTREE_ROOT,
    MalformedAnswerError,
    codex_source_git_subpath,
    evaluate,
)
from teatree.agents.harness_registry import (
    HarnessBuildContext,
    HarnessCapabilities,
    HarnessSpec,
    NoAvailableHarnessError,
    register_harness,
    select_harness,
)
from tests.teatree_agents._executor_fakes import (
    EXAMPLE_ROOT,
    EXECUTOR_ID,
    FakeExecutor,
    attested,
    executor_fixture,
    mount,
    probe_fixture,
    worker_fixture,
)

pytestmark = pytest.mark.usefixtures("pass_store")

_CHECKOUT = "/home/<user>/teatree-deploy"
_CHECKOUT_GIT = f"{_CHECKOUT}/.git"


@pytest.fixture
def fake(tmp_path: Path) -> FakeExecutor:
    return FakeExecutor(tmp_path)


def _applied(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> FakeExecutor:
    for key, value in fake.install().items():
        monkeypatch.setenv(key, value)
    return fake


def _refusal(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, phase: str = "planning") -> str | None:
    _applied(fake, monkeypatch)
    return read_only_executor_refusal(HarnessBuildContext(phase=phase))


def _assert_started_nothing(fake: FakeExecutor) -> None:
    assert all(
        call[0] in {"inspect", "ps"}
        or "/usr/bin/python3" in call
        or call[-3:] == ["test", "-d", f"{CODEX_HOME_ROOT}/{codex_source_git_subpath()}"]
        for call in fake.calls
    )


def test_a_passing_executor_is_attested_for_the_roots_it_was_probed_with(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    _applied(fake, monkeypatch)

    attestation = attest("planning", [EXAMPLE_ROOT])

    assert attestation.container_id == EXECUTOR_ID
    assert [request["roots"] for request in fake.probe_requests] == [[EXAMPLE_ROOT]]


def test_deploy_git_is_repaired_only_when_both_mounts_name_the_same_source(tmp_path: Path) -> None:
    checkout = tmp_path / "deploy"
    (checkout / ".git").mkdir(parents=True)
    worker = worker_fixture()
    executor = executor_fixture("pass")
    worker["CheckoutEnv"] = [f"TEATREE_DEPLOY_CHECKOUT={checkout}"]
    worker["Mounts"].append({"Type": "bind", "Source": str(checkout), "Destination": str(checkout), "RW": True})
    executor["Mounts"].append(
        {"Type": "bind", "Source": str(checkout / ".git"), "Destination": str(checkout / ".git"), "RW": False}
    )

    with patch("teatree.agents.codex_read_only_executor.repair_git_metadata") as repair:
        codex_read_only_executor._repair_mounted_git(worker, executor, ())
        repair.assert_called_once()
        repair.reset_mock()
        executor["Mounts"][-1]["Source"] = str(tmp_path / "unrelated")
        codex_read_only_executor._repair_mounted_git(worker, executor, ())
        repair.assert_not_called()


def test_checkout_git_repair_requires_matching_clone_and_worktree_mounts(tmp_path: Path) -> None:
    root = tmp_path / "clones"
    worktrees = tmp_path / "worktrees"
    checkout = root / "repo"
    (checkout / ".git").mkdir(parents=True)
    worker = worker_fixture()
    executor = executor_fixture("pass")
    for target in (root, worktrees):
        worker["Mounts"].append({"Type": "bind", "Source": str(target), "Destination": str(target), "RW": True})
        executor["Mounts"].append({"Type": "bind", "Source": str(target), "Destination": str(target), "RW": False})
    with (
        patch.object(codex_read_only_executor, "CLONE_ROOT", str(root)),
        patch.object(codex_read_only_executor, "WORKTREE_ROOT", str(worktrees)),
        patch("teatree.agents.codex_read_only_executor.repair_git_metadata") as repair,
    ):
        codex_read_only_executor._repair_mounted_git(worker, executor, (str(checkout),))
        repair.assert_called_once()
        repair.reset_mock()
        executor["Mounts"][-1]["Source"] = str(tmp_path / "other")
        codex_read_only_executor._repair_mounted_git(worker, executor, (str(checkout),))
        repair.assert_called_once()
        repair.reset_mock()
        executor["Mounts"][-2]["Source"] = str(tmp_path / "other")
        codex_read_only_executor._repair_mounted_git(worker, executor, (str(checkout),))
        repair.assert_not_called()


def test_source_git_mount_serves_worktree_git_pointer_at_worker_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subpath = "workspace/example/project/.git"
    monkeypatch.setenv("TEATREE_CODEX_SOURCE_GIT_SUBPATH", subpath)
    monkeypatch.setattr("teatree.agents.codex_read_only_verdict._CODEX_SOURCE_GIT_SUBPATH", subpath)
    codex_home = tmp_path / "codex"
    source_git = codex_home / subpath
    linked_git = source_git / "worktrees" / "branch"
    linked_git.mkdir(parents=True)
    (linked_git / "commondir").write_text("../..\n", encoding="utf-8")
    worktrees = tmp_path / "worktrees"
    checkout = worktrees / "branch"
    checkout.mkdir(parents=True)
    (checkout / ".git").write_text(f"gitdir: {linked_git}\n", encoding="utf-8")
    worker = worker_fixture()
    executor = executor_fixture("pass")
    worker["Mounts"].extend(
        [
            {"Type": "volume", "Source": str(codex_home), "Destination": str(codex_home), "RW": True},
            {"Type": "bind", "Source": str(worktrees), "Destination": str(worktrees), "RW": True},
        ]
    )
    executor["Mounts"].extend(
        [
            {"Type": "volume", "Source": str(source_git), "Destination": str(source_git), "RW": False},
            {"Type": "bind", "Source": str(worktrees), "Destination": str(worktrees), "RW": False},
        ]
    )

    with (
        patch.object(codex_read_only_executor, "CODEX_HOME_ROOT", str(codex_home)),
        patch.object(codex_read_only_executor, "WORKTREE_ROOT", str(worktrees)),
    ):
        codex_read_only_executor._repair_mounted_git(worker, executor, (str(checkout),))


def test_attest_repairs_mounted_git_before_the_probe(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    _applied(fake, monkeypatch)
    with patch("teatree.agents.codex_read_only_executor._repair_mounted_git") as repair:
        attest("planning", [EXAMPLE_ROOT])

    repair.assert_called_once_with(fake.worker, fake.executor, [EXAMPLE_ROOT])


def test_writable_mount_refuses_with_named_reason_and_starts_nothing(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.probe["writes"][WORKTREE_ROOT] = "OK"

    reason = _refusal(fake, monkeypatch)

    assert reason is not None
    assert f"{WORKTREE_ROOT} accepted a write" in reason
    assert "bars write tools" in reason
    assert "cannot enforce" in reason
    _assert_started_nothing(fake)


def test_permission_denied_is_not_read_only(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.probe["writes"][CLONE_ROOT] = "EACCES"

    assert f"{CLONE_ROOT} is not a read-only mount (EACCES)" in str(_refusal(fake, monkeypatch))


def test_docker_file_mounts_may_answer_eacces_only_while_mounted_read_only(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _refusal(fake, monkeypatch) is None
    hosts = next(item for item in fake.probe["mounts"] if item["target"] == "/etc/hosts")
    hosts["options"] = "rw,relatime"

    assert "/etc/hosts is not a read-only mount (EACCES)" in str(_refusal(fake, monkeypatch))


def test_unlisted_mount_is_refused_even_when_read_only(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.probe["mounts"].append(mount("/var/run/docker.sock"))
    fake.probe["writes"]["/var/run/docker.sock"] = "EROFS"

    assert "unexpected mount /var/run/docker.sock (ext4)" in str(_refusal(fake, monkeypatch))


def test_no_successful_control_write_refuses(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.probe["controls"][EXECUTOR_HOME_ROOT] = "EROFS"

    assert f"{EXECUTOR_HOME_ROOT} is not a writable tmpfs" in str(_refusal(fake, monkeypatch))


@pytest.mark.parametrize(("cap_eff", "no_new_privs"), [("00000000a80425fb", "1"), ("0000000000000000", "0")])
def test_capable_executor_is_refused(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, cap_eff: str, no_new_privs: str
) -> None:
    fake.probe["cap_eff"], fake.probe["no_new_privs"] = cap_eff, no_new_privs

    assert f"it runs with capabilities {cap_eff} and no_new_privs {no_new_privs}" in str(_refusal(fake, monkeypatch))


def test_secret_path_in_pid1_environ_is_refused(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.probe["pid1_env"].append("PASSWORD_STORE_DIR")

    assert "its environment carries PASSWORD_STORE_DIR" in str(_refusal(fake, monkeypatch))


def test_forbidden_tool_on_path_is_refused(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.probe["tools"]["gh"] = True

    assert "gh resolves on its PATH" in str(_refusal(fake, monkeypatch))


def test_tmpfs_backed_bind_at_an_allowed_target_is_refused(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.executor["HostConfig"]["Tmpfs"] = {**fake.executor["HostConfig"]["Tmpfs"], WORKTREE_ROOT: "uid=65534"}
    fake.executor["Mounts"] = [item for item in fake.executor["Mounts"] if item["Destination"] != WORKTREE_ROOT]

    assert f"its tmpfs set is ['{WORKTREE_ROOT}'" in str(_refusal(fake, monkeypatch))


def test_absent_declared_target_refuses(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.probe["mounts"] = [item for item in fake.probe["mounts"] if item["target"] != WORKTREE_ROOT]
    del fake.probe["writes"][WORKTREE_ROOT]

    assert f"{WORKTREE_ROOT} is not mounted in the executor" in str(_refusal(fake, monkeypatch))


def _without_tools(fake: FakeExecutor) -> None:
    fake.raw_probe = {key: value for key, value in fake.probe.items() if key != "tools"}


def _with_extra_field(fake: FakeExecutor) -> None:
    fake.raw_probe = {**fake.probe, "extra": True}


def _with_wrong_type(fake: FakeExecutor) -> None:
    fake.raw_probe = {**fake.probe, "cap_eff": 0}


def _unparseable(fake: FakeExecutor) -> None:
    fake.raw_probe = "Traceback (most recent call last):"


def _non_zero_exit(fake: FakeExecutor) -> None:
    fake.probe_exit = 1


@pytest.mark.parametrize(
    ("corrupt", "reason"),
    [
        (_unparseable, "answered something that is not JSON"),
        (_without_tools, "its answer is malformed (probe)"),
        (_with_extra_field, "its answer is malformed (probe)"),
        (_with_wrong_type, "its answer is malformed (cap_eff)"),
        (_non_zero_exit, "docker exec exited 1"),
    ],
    ids=["unparsable", "missing-field", "extra-field", "wrong-type", "non-zero-exit"],
)
def test_malformed_or_partial_probe_output_refuses(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, corrupt: Callable[[FakeExecutor], None], reason: str
) -> None:
    corrupt(fake)

    assert reason in str(_refusal(fake, monkeypatch))


def test_control_db_volume_at_an_allowed_target_is_refused(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    clones = next(item for item in fake.executor["Mounts"] if item["Destination"] == CLONE_ROOT)
    clones["Name"] = "example_control_db"
    clones["Source"] = "/var/lib/docker/volumes/example_control_db/_data"

    assert f"its mount {CLONE_ROOT} is not the worker's {CLONE_ROOT}" in str(_refusal(fake, monkeypatch))


def test_rw_inspect_mount_is_refused(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    next(item for item in fake.executor["Mounts"] if item["Destination"] == WORKTREE_ROOT)["RW"] = True

    assert f"its mount {WORKTREE_ROOT} is read-write" in str(_refusal(fake, monkeypatch))


def test_codex_source_git_mount_must_match_the_workers_volume(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_git = next(item for item in fake.executor["Mounts"] if item["Destination"] == CODEX_SOURCE_GIT)
    source_git["Source"] = "/srv/unrelated/.git"

    assert f"its mount {CODEX_SOURCE_GIT} is not the worker's {CODEX_SOURCE_GIT}" in str(_refusal(fake, monkeypatch))


def test_codex_source_git_mount_is_write_probed(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.probe["writes"][CODEX_SOURCE_GIT] = "OK"

    assert f"{CODEX_SOURCE_GIT} accepted a write" in str(_refusal(fake, monkeypatch))


def test_source_git_subpath_cannot_escape_the_codex_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.agents.codex_read_only_verdict._CODEX_SOURCE_GIT_SUBPATH", "../private/.git")

    with pytest.raises(MalformedAnswerError, match="TEATREE_CODEX_SOURCE_GIT_SUBPATH"):
        codex_source_git_subpath()


def test_an_empty_source_git_subpath_means_no_source_clone(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.codex_source_git_present = False
    fake.executor["Mounts"] = [item for item in fake.executor["Mounts"] if item["Destination"] != CODEX_SOURCE_GIT]
    fake.probe["mounts"] = [item for item in fake.probe["mounts"] if item["target"] != CODEX_SOURCE_GIT]
    fake.probe["writes"].pop(CODEX_SOURCE_GIT)
    fake.probe["scan"].pop(CODEX_SOURCE_GIT)
    monkeypatch.setattr("teatree.agents.codex_read_only_verdict._CODEX_SOURCE_GIT_SUBPATH", "")
    _applied(fake, monkeypatch)

    assert read_only_executor_refusal(HarnessBuildContext(phase="planning")) is None


def test_a_malformed_source_git_subpath_refuses_instead_of_raising(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    _applied(fake, monkeypatch)
    monkeypatch.setattr("teatree.agents.codex_read_only_verdict._CODEX_SOURCE_GIT_SUBPATH", "../private/.git")

    assert "TEATREE_CODEX_SOURCE_GIT_SUBPATH" in str(read_only_executor_refusal(HarnessBuildContext(phase="planning")))


def test_worker_git_check_failure_is_not_treated_as_an_absent_clone(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.codex_source_git_error = True
    fake.install()

    assert "Codex source Git path could not be checked" in str(_refusal(fake, monkeypatch))


def test_executor_without_a_codex_source_clone_keeps_the_original_mount_contract() -> None:
    executor = executor_fixture("pass")
    executor["Mounts"] = [mount for mount in executor["Mounts"] if mount["Destination"] != CODEX_SOURCE_GIT]
    observation = probe_fixture("pass")
    observation["mounts"] = [mount for mount in observation["mounts"] if mount["target"] != CODEX_SOURCE_GIT]
    observation["writes"].pop(CODEX_SOURCE_GIT)
    observation["scan"].pop(CODEX_SOURCE_GIT)

    assert evaluate(observation, executor, worker_fixture(), (EXAMPLE_ROOT,), codex_source_git_present=False) is None


def test_undeclared_inspect_mount_is_refused(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.executor["Mounts"].append({"Type": "bind", "Source": "/srv", "Destination": "/srv", "RW": False})

    assert "it mounts /srv, which is not a declared read-only mount" in str(_refusal(fake, monkeypatch))


def test_writable_rootfs_is_refused(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.executor["HostConfig"]["ReadonlyRootfs"] = False

    assert "its root filesystem is writable" in str(_refusal(fake, monkeypatch))


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("Privileged", True, "it is privileged"),
        ("CapAdd", ["SYS_ADMIN"], "it adds Linux capabilities"),
        ("CapDrop", [], "it keeps Linux capabilities"),
        ("SecurityOpt", [], "its security options are [], not ['no-new-privileges:true']"),
        ("SecurityOpt", ["no-new-privileges:false"], "its security options are ['no-new-privileges:false']"),
        (
            "SecurityOpt",
            ["no-new-privileges:true", "seccomp=unconfined"],
            "its security options are ['no-new-privileges:true', 'seccomp=unconfined']",
        ),
        ("PidMode", "host", "it shares the Pid host namespace"),
        ("PidMode", "container:another", "it shares the Pid container:another namespace"),
        ("IpcMode", "host", "it shares the Ipc host namespace"),
        ("IpcMode", "shareable", "it shares the Ipc shareable namespace"),
        ("UsernsMode", "host", "it shares the Userns host namespace"),
        ("Devices", [{"PathOnHost": "/dev/fuse"}], "it maps host devices"),
    ],
)
def test_privileged_or_capadd_executor_is_refused(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, key: str, value: object, reason: str
) -> None:
    fake.executor["HostConfig"][key] = value

    assert reason in str(_refusal(fake, monkeypatch))


@pytest.mark.parametrize("network", ["host", "container:another", "bridge", "teatree_default"])
def test_an_executor_off_its_own_network_is_refused(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, network: str
) -> None:
    fake.executor["HostConfig"]["NetworkMode"] = network

    assert f"it is on the {network} network, not teatree_ro-executor" in str(_refusal(fake, monkeypatch))


def test_a_mount_the_write_probe_skipped_refuses(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    del fake.probe["writes"][CLONE_ROOT]

    assert f"the write probe skipped {CLONE_ROOT}" in str(_refusal(fake, monkeypatch))


def test_a_device_mount_outside_the_runtime_set_is_refused(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.probe["mounts"].append(mount("/dev/extra", fstype="tmpfs", options="rw,nosuid"))

    assert "unexpected mount /dev/extra (tmpfs)" in str(_refusal(fake, monkeypatch))


def test_a_readable_secret_file_under_a_mount_is_refused(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.probe["scan"][CLONE_ROOT]["secrets"] = [f"{CLONE_ROOT}/example/.env"]

    assert f"it can read {CLONE_ROOT}/example/.env, a secret file under {CLONE_ROOT}" in str(
        _refusal(fake, monkeypatch)
    )


@pytest.mark.parametrize("target", ["/", _CHECKOUT_GIT, CLONE_ROOT, WORKTREE_ROOT])
def test_a_mount_the_secret_scan_skipped_refuses(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    del fake.probe["scan"][target]

    assert f"the secret scan skipped {target}" in str(_refusal(fake, monkeypatch))


def test_an_executor_running_as_root_is_refused(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.probe["uid"] = 0

    assert "it runs as root" in str(_refusal(fake, monkeypatch))


def test_an_executor_that_owns_a_mounted_tree_is_refused(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.probe["uid"] = fake.probe["scan"][CLONE_ROOT]["owner"]

    assert f"it runs as uid {fake.probe['uid']}, which owns {_CHECKOUT_GIT}" in str(_refusal(fake, monkeypatch))


def test_the_whole_deploy_checkout_mounted_is_refused(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = next(item for item in fake.executor["Mounts"] if item["Destination"] == _CHECKOUT_GIT)
    checkout["Source"] = checkout["Destination"] = _CHECKOUT

    assert f"it mounts {_CHECKOUT}, which is not a declared read-only mount" in str(_refusal(fake, monkeypatch))


def test_a_checkout_git_mount_from_another_source_is_refused(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    next(item for item in fake.executor["Mounts"] if item["Destination"] == _CHECKOUT_GIT)["Source"] = "/srv/.git"

    assert f"its mount {_CHECKOUT_GIT} is not the worker's {_CHECKOUT_GIT}" in str(_refusal(fake, monkeypatch))


def test_a_checkout_whose_git_data_it_cannot_read_is_refused(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.probe["roots"][EXAMPLE_ROOT]["git"] = f"{_CHECKOUT_GIT}/packed-refs is unreadable"
    _applied(fake, monkeypatch)

    with pytest.raises(ExecutorRefusalError, match=f"git cannot read the checkout {EXAMPLE_ROOT} there"):
        attest("planning", [EXAMPLE_ROOT])


@pytest.mark.parametrize("write", ["OK", "EACCES"])
def test_a_checkout_on_a_mount_that_is_not_read_only_is_refused(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    fake.probe["roots"][EXAMPLE_ROOT]["write"] = write
    _applied(fake, monkeypatch)

    with pytest.raises(
        ExecutorRefusalError, match=rf"{EXAMPLE_ROOT} is not a directory on a read-only mount there \({write}\)"
    ):
        attest("planning", [EXAMPLE_ROOT])


@pytest.mark.parametrize(
    "checkout_env",
    [[], ["TEATREE_DEPLOY_CHECKOUT=relative/checkout"], [f"TEATREE_DEPLOY_CHECKOUT={_CHECKOUT}"] * 2],
    ids=["none", "relative", "twice"],
)
def test_a_worker_without_exactly_one_absolute_deploy_checkout_is_refused(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, checkout_env: list[str]
) -> None:
    fake.worker["CheckoutEnv"] = checkout_env

    assert "its answer is malformed (worker deploy checkout)" in str(_refusal(fake, monkeypatch))


def test_a_requested_checkout_outside_the_read_only_mounts_is_refused(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    _applied(fake, monkeypatch)

    with pytest.raises(
        ExecutorRefusalError, match=f"the checkout /srv/elsewhere is not a directory.*re-home.*{WORKTREE_ROOT}"
    ):
        attest("planning", ["/srv/elsewhere"])


def test_attestation_cannot_be_constructed_outside_attest() -> None:
    with pytest.raises(TypeError, match="issued only by a passing live probe"):
        ExecutorAttestation(EXECUTOR_ID, "planning", datetime.now(UTC), object())


def test_an_attestation_cannot_be_reissued_for_another_container_or_phase(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    attestation = attested(fake, monkeypatch)

    with pytest.raises(TypeError, match="issued only by a passing live probe"):
        dataclasses.replace(attestation, container_id="container-02")
    with pytest.raises(TypeError, match="issued only by a passing live probe"):
        copy.replace(attestation, phase="coding")


class _StrictEnviron(MutableMapping[str, str]):
    """Fails the test on a read of any variable but the two the verdict may consult."""

    allowed = frozenset({"PATH", "PASSWORD_STORE_DIR"})

    def __init__(self, values: Iterable[tuple[str, str]]) -> None:
        self._values = dict(values)

    def __getitem__(self, key: str) -> str:
        if key not in self.allowed:
            pytest.fail(f"the read-only verdict read the environment variable {key!r}")
        return self._values[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._values[key] = value

    def __delitem__(self, key: str) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


@pytest.fixture
def strict_environ(fake: FakeExecutor) -> Iterator[_StrictEnviron]:
    environ = _StrictEnviron(fake.install().items())
    with (
        patch.object(os, "environ", environ),
        patch("teatree.config.get_effective_settings", side_effect=AssertionError("settings read")),
        patch("teatree.core.models.ConfigSetting.objects", new=None),
    ):
        yield environ


@pytest.mark.usefixtures("strict_environ")
def test_verdict_reads_no_env_flag_or_setting(fake: FakeExecutor) -> None:
    assert read_only_executor_refusal(HarnessBuildContext(phase="planning")) is None
    assert len(fake.probe_requests) == 1


def _managed_spec() -> HarnessSpec:
    return HarnessSpec(
        name="managed_executor_test",
        factory=lambda ctx: pytest.fail("selection never builds"),
        capabilities=HarnessCapabilities(managed_lane=True),
        read_only_executor_refusal=read_only_executor_refusal,
    )


@pytest.fixture
def managed() -> Iterator[str]:
    register_harness(_managed_spec())
    yield "managed_executor_test"
    harness_registry._REGISTRY.pop("managed_executor_test", None)


def test_selection_runs_a_live_probe_exec(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, managed: str) -> None:
    _applied(fake, monkeypatch)

    for _ in range(2):
        assert select_harness([managed], HarnessBuildContext(phase="planning")).spec.name == managed

    probes = [call for call in fake.calls if call[:2] == ["exec", "-i"] and "/usr/bin/python3" in call]
    assert [call[2] for call in probes] == [EXECUTOR_ID, EXECUTOR_ID]


@pytest.mark.parametrize("phase", ["reviewing", "critic_reviewing"])
def test_review_phases_are_probed_on_each_selection(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, managed: str, phase: str
) -> None:
    _applied(fake, monkeypatch)

    assert phase in EXECUTOR_PHASES
    assert select_harness([managed], HarnessBuildContext(phase=phase)).spec.name == managed
    fake.probe["writes"]["/"] = "OK"
    fake.install()
    with pytest.raises(NoAvailableHarnessError, match="/ accepted a write"):
        select_harness([managed], HarnessBuildContext(phase=phase))

    assert len(fake.probe_requests) == 2


def _no_login(fake: FakeExecutor) -> None:
    fake.with_login = False


def _no_container(fake: FakeExecutor) -> None:
    fake.containers = []


def _two_containers(fake: FakeExecutor) -> None:
    fake.containers = [EXECUTOR_ID, "container-99"]


def _probe_fails(fake: FakeExecutor) -> None:
    fake.probe["writes"]["/"] = "OK"


def _daemon_refuses(fake: FakeExecutor) -> None:
    fake.inspect_exit = 1


def _unchanged(fake: FakeExecutor) -> None:
    del fake


class _BarCase(NamedTuple):
    phase: str
    breakage: Callable[[FakeExecutor], None]
    detail: str


@pytest.mark.parametrize(
    "case",
    [
        _BarCase("planning", _no_login, "it has no Codex login: run `t3 codex auth import --executor`"),
        _BarCase("planning", _daemon_refuses, "docker inspect exited 1: Cannot connect to the Docker daemon"),
        _BarCase("planning", _no_container, f"0 running {EXECUTOR_SERVICE} containers"),
        _BarCase("planning", _two_containers, f"2 running {EXECUTOR_SERVICE} containers"),
        _BarCase("planning", _probe_fails, f"{EXECUTOR_SERVICE} is not read-only: / accepted a write"),
        _BarCase("bughunt", _unchanged, "the read-only executor serves only"),
        _BarCase("shipping", _unchanged, "the read-only executor serves only"),
    ],
    ids=[
        "no-login",
        "no-docker",
        "no-container",
        "two-containers",
        "probe-fails",
        "bughunt",
        "shipping",
    ],
)
def test_barred_without_a_passing_executor(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, managed: str, case: _BarCase
) -> None:
    case.breakage(fake)
    _applied(fake, monkeypatch)

    with pytest.raises(NoAvailableHarnessError) as refused:
        select_harness([managed], HarnessBuildContext(phase=case.phase))

    (rejection,) = refused.value.rejected
    assert rejection.reason.startswith(
        f"phase {case.phase!r} bars write tools, which a managed-lane harness cannot enforce"
    )
    assert case.detail in rejection.reason


def test_a_missing_docker_cli_is_named(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _applied(fake, monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    assert "the docker CLI is not installed here" in str(
        read_only_executor_refusal(HarnessBuildContext(phase="planning"))
    )
