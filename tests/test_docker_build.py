"""Tests for teatree.docker.build — single master base-image tag + build-when-absent-or-broken."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teatree.docker.build import ensure_base_image
from teatree.types import BaseImageConfig


@pytest.fixture
def base_image_cfg(tmp_path: Path) -> BaseImageConfig:
    (tmp_path / "Dockerfile-local").write_text("FROM scratch\n")
    (tmp_path / "uv.lock").write_text("lock-contents\n")
    return BaseImageConfig(
        image_name="myapp-local",
        dockerfile="Dockerfile-local",
        lockfile="uv.lock",
        build_context=tmp_path,
        env_var="MYAPP_BASE_IMAGE",
    )


def test_image_tag_is_the_single_master_tag(base_image_cfg: BaseImageConfig) -> None:
    """The tag is a plain ``{image_name}:base`` — never a per-lockfile sha256 hash."""
    assert base_image_cfg.image_tag() == "myapp-local:base"


def test_image_tag_does_not_change_when_lockfile_changes(base_image_cfg: BaseImageConfig) -> None:
    """A code/dep change does NOT change the tag — the entrypoint uv-sync reconciles drift."""
    tag1 = base_image_cfg.image_tag()
    (base_image_cfg.build_context / base_image_cfg.lockfile).write_text("different-deps\n")
    tag2 = base_image_cfg.image_tag()
    assert tag1 == tag2 == "myapp-local:base"


def _build_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:2] == ["docker", "build"]]


def _rmi_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:2] == ["docker", "rmi"]]


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    allowed: object,
    checked: object,
) -> None:
    monkeypatch.setattr("teatree.docker.build.run_allowed_to_fail", allowed)
    monkeypatch.setattr("teatree.docker.build.run_checked", checked)


def test_reuses_healthy_image_without_rebuilding(
    base_image_cfg: BaseImageConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present, Id-resolvable image is reused — never rebuilt (build-once, reuse-by-all)."""
    calls: list[list[str]] = []

    def fake_allowed(cmd: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(list(cmd))
        # Both probes succeed: tag exists AND its Id resolves → healthy.
        if "--format" in cmd:
            return CompletedProcess(cmd, 0, "sha256:abc123\n", "")
        return CompletedProcess(cmd, 0, "", "")

    def fake_checked(cmd: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(list(cmd))
        return CompletedProcess(cmd, 0, "", "")

    _wire(monkeypatch, allowed=fake_allowed, checked=fake_checked)

    tag = ensure_base_image(base_image_cfg)
    assert tag == "myapp-local:base"
    assert _build_calls(calls) == []
    assert _rmi_calls(calls) == []


def test_code_only_change_does_not_rebuild(
    base_image_cfg: BaseImageConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy image is reused across a code-only worktree — no per-lockfile rebuild."""
    calls: list[list[str]] = []

    def fake_allowed(cmd: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(list(cmd))
        if "--format" in cmd:
            return CompletedProcess(cmd, 0, "sha256:abc123\n", "")
        return CompletedProcess(cmd, 0, "", "")

    _wire(
        monkeypatch,
        allowed=fake_allowed,
        checked=lambda cmd, **_: CompletedProcess(cmd, 0, "", ""),
    )

    ensure_base_image(base_image_cfg)
    # A worktree's lockfile differs from master, but the tag is unchanged and
    # the image is healthy, so no rebuild happens.
    (base_image_cfg.build_context / base_image_cfg.lockfile).write_text("worktree-specific-deps\n")
    ensure_base_image(base_image_cfg)
    assert _build_calls(calls) == []


def test_builds_when_image_absent(
    base_image_cfg: BaseImageConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent image (inspect rc != 0) is built — no rmi (nothing to remove)."""
    calls: list[list[str]] = []

    def fake_allowed(cmd: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(list(cmd))
        return CompletedProcess(cmd, 1, "", "No such image")  # absent

    def fake_checked(cmd: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(list(cmd))
        return CompletedProcess(cmd, 0, "", "")

    _wire(monkeypatch, allowed=fake_allowed, checked=fake_checked)

    tag = ensure_base_image(base_image_cfg)
    assert tag == "myapp-local:base"
    builds = _build_calls(calls)
    assert len(builds) == 1
    assert "-t" in builds[0]
    assert "myapp-local:base" in builds[0]
    assert "-f" in builds[0]
    assert _rmi_calls(calls) == []


def test_rebuilds_when_image_broken(
    base_image_cfg: BaseImageConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken image (tag resolves but Id is empty) is force-removed then rebuilt.

    Models a corrupt/interrupted build: ``docker image inspect TAG`` succeeds
    (the tag is present) but ``inspect TAG --format '{{.Id}}'`` returns an empty
    Id, so the config cannot be resolved. ``ensure_base_image`` must ``rmi -f``
    then ``build``.
    """
    calls: list[list[str]] = []

    def fake_allowed(cmd: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(list(cmd))
        if "--format" in cmd:
            # Tag present but Id unresolvable → broken.
            return CompletedProcess(cmd, 0, "\n", "")
        return CompletedProcess(cmd, 0, "", "")  # bare inspect: tag exists

    def fake_checked(cmd: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(list(cmd))
        return CompletedProcess(cmd, 0, "", "")

    _wire(monkeypatch, allowed=fake_allowed, checked=fake_checked)

    tag = ensure_base_image(base_image_cfg)
    assert tag == "myapp-local:base"
    rmis = _rmi_calls(calls)
    assert len(rmis) == 1
    assert "-f" in rmis[0]
    assert "myapp-local:base" in rmis[0]
    builds = _build_calls(calls)
    assert len(builds) == 1
    assert "myapp-local:base" in builds[0]


def test_rebuilds_when_id_probe_errors(
    base_image_cfg: BaseImageConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tag present but the Id probe itself errors (rc != 0) is also treated as broken."""
    calls: list[list[str]] = []

    def fake_allowed(cmd: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(list(cmd))
        if "--format" in cmd:
            return CompletedProcess(cmd, 1, "", "inspect error")
        return CompletedProcess(cmd, 0, "", "")  # bare inspect: tag exists

    def fake_checked(cmd: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(list(cmd))
        return CompletedProcess(cmd, 0, "", "")

    _wire(monkeypatch, allowed=fake_allowed, checked=fake_checked)

    ensure_base_image(base_image_cfg)
    assert len(_rmi_calls(calls)) == 1
    assert len(_build_calls(calls)) == 1


def test_passes_build_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Build-args are forwarded as --build-arg KEY=VALUE pairs on the build."""
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "uv.lock").write_text("lock-contents\n")
    cfg = BaseImageConfig(
        image_name="svc",
        dockerfile="Dockerfile",
        lockfile="uv.lock",
        build_context=tmp_path,
        env_var="SVC_IMAGE",
        build_args={"PYTHON_VERSION": "3.12", "FOO": "bar"},
    )

    captured: list[list[str]] = []
    _wire(
        monkeypatch,
        allowed=lambda cmd, **_: CompletedProcess(cmd, 1, "", ""),  # absent → build
        checked=lambda cmd, **_: (captured.append(list(cmd)), CompletedProcess(cmd, 0, "", ""))[1],
    )

    ensure_base_image(cfg)
    build_cmd = captured[0]
    assert "--build-arg" in build_cmd
    assert "PYTHON_VERSION=3.12" in build_cmd
    assert "FOO=bar" in build_cmd
