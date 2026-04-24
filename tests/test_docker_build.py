"""Tests for teatree.docker.build — lockfile-keyed image tagging and build cache."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teatree.docker.build import ensure_base_image, image_tag_for_lockfile
from teatree.types import BaseImageConfig


@pytest.fixture
def base_image_cfg(tmp_path: Path) -> BaseImageConfig:
    (tmp_path / "Dockerfile-local").write_text("FROM scratch\n")
    (tmp_path / "Pipfile.lock").write_text('{"_meta": {"hash": "abc"}}\n')
    return BaseImageConfig(
        image_name="myapp-local",
        dockerfile="Dockerfile-local",
        lockfile="Pipfile.lock",
        build_context=tmp_path,
        env_var="MYAPP_BASE_IMAGE",
    )


def test_image_tag_is_deterministic_for_same_lockfile(base_image_cfg: BaseImageConfig):
    tag1 = image_tag_for_lockfile(base_image_cfg)
    tag2 = image_tag_for_lockfile(base_image_cfg)
    assert tag1 == tag2
    assert tag1.startswith("myapp-local:deps-")
    assert len(tag1.split(":deps-")[1]) == 12


def test_image_tag_changes_when_lockfile_changes(base_image_cfg: BaseImageConfig):
    tag1 = image_tag_for_lockfile(base_image_cfg)
    (base_image_cfg.build_context / base_image_cfg.lockfile).write_text('{"_meta": {"hash": "different"}}\n')
    tag2 = image_tag_for_lockfile(base_image_cfg)
    assert tag1 != tag2


def test_ensure_base_image_skips_build_when_image_exists(
    base_image_cfg: BaseImageConfig, monkeypatch: pytest.MonkeyPatch
):
    """Second call with unchanged lockfile should only probe, never build."""
    calls: list[list[str]] = []

    def fake_allowed(cmd, *, expected_codes=(0,), **_kwargs):
        calls.append(list(cmd))
        # Image exists → return rc=0, no build should follow.
        return CompletedProcess(cmd, 0, "", "")

    def fake_checked(cmd, **_kwargs):
        calls.append(list(cmd))
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("teatree.docker.build.run_allowed_to_fail", fake_allowed)
    monkeypatch.setattr("teatree.docker.build.run_checked", fake_checked)

    tag = ensure_base_image(base_image_cfg)
    assert tag.startswith("myapp-local:deps-")
    assert len(calls) == 1
    assert calls[0][:3] == ["docker", "image", "inspect"]


def test_ensure_base_image_builds_when_missing(base_image_cfg: BaseImageConfig, monkeypatch: pytest.MonkeyPatch):
    """First call for a new lockfile hash should probe then build."""
    calls: list[list[str]] = []

    def fake_allowed(cmd, *, expected_codes=(0,), **_kwargs):
        calls.append(list(cmd))
        return CompletedProcess(cmd, 1, "", "No such image")  # miss

    def fake_checked(cmd, **_kwargs):
        calls.append(list(cmd))
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("teatree.docker.build.run_allowed_to_fail", fake_allowed)
    monkeypatch.setattr("teatree.docker.build.run_checked", fake_checked)

    tag = ensure_base_image(base_image_cfg)
    assert len(calls) == 2
    assert calls[0][:3] == ["docker", "image", "inspect"]
    assert calls[1][:2] == ["docker", "build"]
    assert "-t" in calls[1]
    assert tag in calls[1]
    assert "-f" in calls[1]


def test_ensure_base_image_passes_build_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build-args are forwarded as --build-arg KEY=VALUE pairs."""
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
    monkeypatch.setattr(
        "teatree.docker.build.run_allowed_to_fail",
        lambda cmd, **_: CompletedProcess(cmd, 1, "", ""),
    )
    monkeypatch.setattr(
        "teatree.docker.build.run_checked",
        lambda cmd, **_: (captured.append(list(cmd)), CompletedProcess(cmd, 0, "", ""))[1],
    )

    ensure_base_image(cfg)
    build_cmd = captured[0]
    assert "--build-arg" in build_cmd
    assert "PYTHON_VERSION=3.12" in build_cmd
    assert "FOO=bar" in build_cmd
