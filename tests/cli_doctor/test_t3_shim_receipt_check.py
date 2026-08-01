# test-path: cross-cutting
"""``_check_t3_shim_receipt`` + the expected-checkout / repair primitives (#3231).

A second, unrelated ``uv tool install --editable <other-checkout>`` under the
same ``teatree`` entrypoint name silently steals the global ``t3`` shim (and a
moved checkout re-points the receipt at a stale path). The check resolves the
active shim's ``uv-receipt.toml`` editable source and FAILs — with a ``--repair``
that re-points it — when it does not match the ``teatree`` checkout under
``$T3_REPO``.

Covers both layouts: a plain clone (``$T3_REPO`` builds ``teatree``) and a fork
that vendors core at ``$T3_REPO/vendor/teatree`` (the root builds a different
distribution, so the vendored subdir is the checkout and the root rides along as
``--with-editable``).

Cross-cutting: the doctor check lives in ``teatree.cli`` while the detection +
repair primitives live in ``teatree.utils.editable_pth``.
"""

import io
import subprocess
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from teatree.cli.doctor.checks_environment import _check_t3_shim_receipt
from teatree.utils import editable_pth
from teatree.utils.editable_pth import (
    EditableInstall,
    expected_editable_install,
    host_install_missing,
    host_root_for_checkout,
    repair_editable_install,
)


def _write_receipt(tool_dir: Path, editable: Path, host: Path | None = None) -> None:
    """Write a uv tool receipt recording *editable* — and *host* when co-installed.

    uv records a ``--with-editable`` co-install as its own ``requirements`` entry
    beside ``teatree``, which is what makes the fork root's presence observable.
    """
    requirements = [f'{{ name = "teatree", editable = "{editable}" }}']
    if host is not None:
        requirements.append(f'{{ name = "{host.name}", editable = "{host}" }}')
    (tool_dir / "teatree").mkdir(parents=True, exist_ok=True)
    (tool_dir / "teatree" / "uv-receipt.toml").write_text(
        "[tool]\nrequirements = [" + ", ".join(requirements) + "]\n",
        encoding="utf-8",
    )


def _make_receipt(tmp_path: Path, editable: Path, host: Path | None = None) -> Path:
    """Build a uv-tool dir whose teatree receipt records *editable* as its source."""
    tool_dir = tmp_path / "uvtools"
    _write_receipt(tool_dir, editable, host)
    return tool_dir


def _make_project(root: Path, name: str) -> Path:
    """Create a directory whose ``pyproject.toml`` builds the *name* distribution."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")
    return root


def _make_vendored_fork(tmp_path: Path) -> tuple[Path, Path]:
    """Create a fork repo that vendors the ``teatree`` distribution under ``vendor/teatree``."""
    root = _make_project(tmp_path / "downstream-fork", "downstream-fork")
    return root, _make_project(root / "vendor" / "teatree", "teatree")


def _uv_install_stub(tool_dir: Path, calls: list[list[str]], *, moves_receipt: bool = True) -> Callable[..., object]:
    """Stand in for ``uv tool install``: it rewrites the receipt only when it really installs.

    ``moves_receipt=False`` reproduces uv aimed at a checkout that builds a
    different distribution — it exits, the ``teatree`` receipt never moves.
    """

    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if moves_receipt:
            host = Path(argv[argv.index("--with-editable") + 1]) if "--with-editable" in argv else None
            _write_receipt(tool_dir, Path(argv[argv.index("--editable") + 1]), host)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    return _run


def _run(**kwargs: bool) -> tuple[bool, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        ok = _check_t3_shim_receipt(**kwargs)
    return ok, out.getvalue()


class TestCheckT3ShimReceipt:
    def test_passes_when_receipt_matches_expected_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkout = tmp_path / "teatree-clone"
        checkout.mkdir()
        tool_dir = _make_receipt(tmp_path, checkout)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.setenv("T3_REPO", str(checkout))

        ok, message = _run()

        assert ok is True
        assert "FAIL" not in message

    def test_fails_when_shim_hijacked_by_unrelated_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected = tmp_path / "teatree-clone"
        expected.mkdir()
        hijacker = tmp_path / "unrelated-checkout"  # a real, existing but WRONG clone
        hijacker.mkdir()
        tool_dir = _make_receipt(tmp_path, hijacker)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.setenv("T3_REPO", str(expected))

        ok, message = _run()

        assert ok is False
        assert "FAIL" in message
        assert str(hijacker) in message
        assert str(expected.resolve()) in message
        assert "--repair" in message

    def test_repair_repoints_and_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        expected = tmp_path / "teatree-clone"
        expected.mkdir()
        hijacker = tmp_path / "unrelated-checkout"
        hijacker.mkdir()
        tool_dir = _make_receipt(tmp_path, hijacker)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.setenv("T3_REPO", str(expected))

        calls: list[EditableInstall] = []

        def _fake_repair(install: EditableInstall) -> bool:
            calls.append(install)
            return True

        monkeypatch.setattr(editable_pth, "repair_editable_install", _fake_repair)

        ok, message = _run(repair=True)

        assert ok is True
        assert calls == [EditableInstall(checkout=expected.resolve(), host=None)]
        assert "Re-pointed" in message
        assert "FAIL" not in message

    def test_skips_when_t3_repo_unset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        hijacker = tmp_path / "unrelated-checkout"
        hijacker.mkdir()
        tool_dir = _make_receipt(tmp_path, hijacker)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.delenv("T3_REPO", raising=False)

        ok, message = _run()

        assert ok is True
        assert "FAIL" not in message

    def test_repair_failure_falls_through_to_fail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        expected = tmp_path / "teatree-clone"
        expected.mkdir()
        hijacker = tmp_path / "unrelated-checkout"
        hijacker.mkdir()
        tool_dir = _make_receipt(tmp_path, hijacker)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.setenv("T3_REPO", str(expected))
        monkeypatch.setattr(editable_pth, "repair_editable_install", lambda _install: False)

        ok, message = _run(repair=True)

        assert ok is False
        assert "FAIL" in message

    def test_repair_that_did_not_take_names_the_manual_command_not_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Telling an operator who just ran `--repair` to run `--repair` is a dead
        # end; the residual FAIL must hand over the raw uv command instead.
        expected = tmp_path / "teatree-clone"
        expected.mkdir()
        hijacker = tmp_path / "unrelated-checkout"
        hijacker.mkdir()
        monkeypatch.setenv("UV_TOOL_DIR", str(_make_receipt(tmp_path, hijacker)))
        monkeypatch.setenv("T3_REPO", str(expected))
        monkeypatch.setattr(editable_pth, "repair_editable_install", lambda _install: False)

        ok, message = _run(repair=True)

        assert ok is False
        assert "t3 doctor check --repair" not in message
        assert f"uv tool install --editable {expected.resolve()} --force" in message

    def test_crash_proof_when_inspection_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> Path:
            msg = "receipt read exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(editable_pth, "receipt_editable_source", _boom)

        ok, message = _run()

        assert ok is True  # an inspection failure warns and passes, never blocks the doctor run
        assert "WARN" in message

    def test_skips_when_no_editable_receipt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A tool dir with no receipt at all → not an editable uv-tool install → skip.
        empty_tool_dir = tmp_path / "uvtools"
        (empty_tool_dir / "teatree").mkdir(parents=True)
        monkeypatch.setenv("UV_TOOL_DIR", str(empty_tool_dir))
        monkeypatch.setenv("T3_REPO", str(tmp_path / "teatree-clone"))

        ok, message = _run()

        assert ok is True
        assert "FAIL" not in message


class TestVendoredCoreLayout:
    """A fork that vendors ``teatree`` under ``vendor/teatree`` (#3231 follow-up).

    ``$T3_REPO`` there builds the fork's own distribution, not ``teatree``. Aiming
    the editable install at the repo root targets a different tool — one with no
    ``t3`` entry point — so uv bails and the shim never moves: ``--repair``
    becomes a no-op that reports itself as a manual ``--repair``.
    """

    def test_healthy_vendored_install_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root, vendored = _make_vendored_fork(tmp_path)
        monkeypatch.setenv("UV_TOOL_DIR", str(_make_receipt(tmp_path, vendored, root)))
        monkeypatch.setenv("T3_REPO", str(root))

        ok, message = _run()

        assert ok is True
        assert "FAIL" not in message

    def test_fails_when_the_fork_root_was_dropped_from_the_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The right checkout with no co-installed fork root: t3 runs, `t3 <overlay> …`
        # still bridges out to the overlay project, and only the IN-PROCESS
        # get_overlay() raises "Overlay not found. Available: t3-teatree". Checking
        # the checkout alone passed this silently.
        root, vendored = _make_vendored_fork(tmp_path)
        monkeypatch.setenv("UV_TOOL_DIR", str(_make_receipt(tmp_path, vendored)))
        monkeypatch.setenv("T3_REPO", str(root))

        ok, message = _run()

        assert ok is False
        assert "FAIL" in message
        assert str(root) in message
        assert "teatree.overlays" in message
        assert f"--editable {vendored} --with-editable {root} --force" in message

    def test_repair_restores_a_dropped_fork_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root, vendored = _make_vendored_fork(tmp_path)
        tool_dir = _make_receipt(tmp_path, vendored)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.setenv("T3_REPO", str(root))

        calls: list[list[str]] = []
        monkeypatch.setattr("shutil.which", lambda _tool: "/usr/bin/uv")
        monkeypatch.setattr("subprocess.run", _uv_install_stub(tool_dir, calls))

        ok, message = _run(repair=True)

        assert ok is True
        assert "FAIL" not in message
        assert calls == [
            ["/usr/bin/uv", "tool", "install", "--editable", str(vendored), "--with-editable", str(root), "--force"]
        ]

    def test_repair_installs_the_vendored_core_with_the_host_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, vendored = _make_vendored_fork(tmp_path)
        _, hijacker = _make_vendored_fork(tmp_path / "stale")
        tool_dir = _make_receipt(tmp_path, hijacker)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.setenv("T3_REPO", str(root))

        calls: list[list[str]] = []
        monkeypatch.setattr("shutil.which", lambda _tool: "/usr/bin/uv")
        monkeypatch.setattr("subprocess.run", _uv_install_stub(tool_dir, calls))

        ok, message = _run(repair=True)

        assert ok is True
        assert "Re-pointed" in message
        assert calls == [
            ["/usr/bin/uv", "tool", "install", "--editable", str(vendored), "--with-editable", str(root), "--force"]
        ]

    def test_repair_that_leaves_the_receipt_put_reports_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, vendored = _make_vendored_fork(tmp_path)
        _, hijacker = _make_vendored_fork(tmp_path / "stale")
        tool_dir = _make_receipt(tmp_path, hijacker)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.setenv("T3_REPO", str(root))
        monkeypatch.setattr("shutil.which", lambda _tool: "/usr/bin/uv")
        monkeypatch.setattr("subprocess.run", _uv_install_stub(tool_dir, [], moves_receipt=False))

        ok, message = _run(repair=True)

        assert ok is False
        assert "FAIL" in message
        assert f"--editable {vendored} --with-editable {root} --force" in message


class TestExpectedEditableInstall:
    def test_plain_clone_is_its_own_checkout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _make_project(tmp_path / "teatree", "teatree")
        monkeypatch.setenv("T3_REPO", str(repo))
        assert expected_editable_install() == EditableInstall(checkout=repo.resolve(), host=None)

    def test_vendored_fork_installs_core_with_the_root_as_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, vendored = _make_vendored_fork(tmp_path)
        monkeypatch.setenv("T3_REPO", str(root))
        assert expected_editable_install() == EditableInstall(checkout=vendored, host=root)

    def test_unidentifiable_repo_falls_back_to_the_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "clone"  # no pyproject.toml at all
        repo.mkdir()
        monkeypatch.setenv("T3_REPO", str(repo))
        assert expected_editable_install() == EditableInstall(checkout=repo.resolve(), host=None)

    def test_returns_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_REPO", raising=False)
        assert expected_editable_install() is None

    def test_returns_none_when_repo_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_REPO", str(tmp_path / "does-not-exist"))
        assert expected_editable_install() is None


class TestHostRootForCheckout:
    """The layout rule every host-native install site shares."""

    def test_vendored_core_yields_the_fork_root(self, tmp_path: Path) -> None:
        root, vendored = _make_vendored_fork(tmp_path)
        assert host_root_for_checkout(vendored) == root

    def test_plain_clone_has_no_host(self, tmp_path: Path) -> None:
        assert host_root_for_checkout(_make_project(tmp_path / "teatree", "teatree")) is None

    def test_vendor_parent_that_is_no_python_project_has_no_host(self, tmp_path: Path) -> None:
        # A bare vendor parent is not a host project — there is nothing to co-install,
        # and `--with-editable` on it would make uv fail outright.
        vendored = _make_project(tmp_path / "plain" / "vendor" / "teatree", "teatree")
        assert host_root_for_checkout(vendored) is None

    def test_checkout_that_does_not_build_teatree_has_no_host(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path / "downstream-fork", "downstream-fork")
        assert host_root_for_checkout(_make_project(root / "vendor" / "teatree", "something-else")) is None


class TestHostInstallMissing:
    def test_false_when_there_is_no_host_to_install(self, tmp_path: Path) -> None:
        assert host_install_missing(EditableInstall(checkout=tmp_path, host=None)) is False

    def test_true_when_the_receipt_records_only_the_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, vendored = _make_vendored_fork(tmp_path)
        monkeypatch.setenv("UV_TOOL_DIR", str(_make_receipt(tmp_path, vendored)))
        assert host_install_missing(EditableInstall(checkout=vendored, host=root)) is True

    def test_false_when_the_receipt_records_the_host_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root, vendored = _make_vendored_fork(tmp_path)
        monkeypatch.setenv("UV_TOOL_DIR", str(_make_receipt(tmp_path, vendored, root)))
        assert host_install_missing(EditableInstall(checkout=vendored, host=root)) is False


class TestRepairEditableInstall:
    def test_returns_false_when_uv_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _tool: None)
        assert repair_editable_install(EditableInstall(checkout=tmp_path, host=None)) is False

    def test_returns_false_when_the_host_did_not_land(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # uv exiting 0 with the checkout in place is not proof the co-install took;
        # reporting success there would leave the overlay unregistered and silent.
        root, vendored = _make_vendored_fork(tmp_path)
        tool_dir = _make_receipt(tmp_path, tmp_path / "stale")
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.setattr("shutil.which", lambda _tool: "/usr/bin/uv")

        def _install_without_the_host(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            _write_receipt(tool_dir, Path(argv[argv.index("--editable") + 1]))
            return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", _install_without_the_host)

        assert repair_editable_install(EditableInstall(checkout=vendored, host=root)) is False

    def test_runs_force_editable_install_and_reports_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkout = tmp_path / "teatree-clone"
        checkout.mkdir()
        tool_dir = _make_receipt(tmp_path, tmp_path / "stale")
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        calls: list[list[str]] = []
        monkeypatch.setattr("shutil.which", lambda _tool: "/usr/bin/uv")
        monkeypatch.setattr("subprocess.run", _uv_install_stub(tool_dir, calls))

        assert repair_editable_install(EditableInstall(checkout=checkout, host=None)) is True
        assert calls == [["/usr/bin/uv", "tool", "install", "--editable", str(checkout), "--force"]]

    def test_returns_false_when_the_receipt_still_points_elsewhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # uv exiting 0 is not proof: it installs BY DISTRIBUTION NAME, so a
        # checkout that does not build ``teatree`` leaves the receipt untouched.
        tool_dir = _make_receipt(tmp_path, tmp_path / "stale")
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.setattr("shutil.which", lambda _tool: "/usr/bin/uv")
        monkeypatch.setattr("subprocess.run", _uv_install_stub(tool_dir, [], moves_receipt=False))

        assert repair_editable_install(EditableInstall(checkout=tmp_path / "elsewhere", host=None)) is False

    def test_returns_false_on_nonzero_exit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="boom")

        monkeypatch.setenv("UV_TOOL_DIR", str(_make_receipt(tmp_path, tmp_path / "stale")))
        monkeypatch.setattr("shutil.which", lambda _tool: "/usr/bin/uv")
        monkeypatch.setattr("subprocess.run", _fake_run)

        assert repair_editable_install(EditableInstall(checkout=tmp_path, host=None)) is False

    def test_returns_false_when_subprocess_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_argv: list[str], **_kwargs: object) -> object:
            msg = "uv missing"
            raise OSError(msg)

        monkeypatch.setattr("shutil.which", lambda _tool: "/usr/bin/uv")
        monkeypatch.setattr("subprocess.run", _boom)

        assert repair_editable_install(EditableInstall(checkout=tmp_path, host=None)) is False
