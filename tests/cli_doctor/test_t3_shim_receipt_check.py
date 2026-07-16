# test-path: cross-cutting
"""``_check_t3_shim_receipt`` + the expected-checkout / repair primitives (#3231).

A second, unrelated ``uv tool install --editable <other-checkout>`` under the
same ``teatree`` entrypoint name silently steals the global ``t3`` shim (and a
moved checkout re-points the receipt at a stale path). The check resolves the
active shim's ``uv-receipt.toml`` editable source and FAILs — with a ``--repair``
that re-points it — when it does not match ``$T3_REPO``.

Cross-cutting: the doctor check lives in ``teatree.cli`` while the detection +
repair primitives live in ``teatree.utils.editable_pth``.
"""

import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from teatree.cli.doctor.checks import _check_t3_shim_receipt
from teatree.utils import editable_pth
from teatree.utils.editable_pth import expected_checkout, repair_receipt_to_checkout


def _make_receipt(tmp_path: Path, editable: Path) -> Path:
    """Build a uv-tool dir whose teatree receipt records *editable* as its source."""
    tool_dir = tmp_path / "uvtools"
    (tool_dir / "teatree").mkdir(parents=True)
    (tool_dir / "teatree" / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "teatree", editable = "' + str(editable) + '" }]\n',
        encoding="utf-8",
    )
    return tool_dir


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

        calls: list[Path] = []

        def _fake_repair(checkout: Path) -> bool:
            calls.append(checkout)
            return True

        monkeypatch.setattr(editable_pth, "repair_receipt_to_checkout", _fake_repair)

        ok, message = _run(repair=True)

        assert ok is True
        assert calls == [expected.resolve()]
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
        monkeypatch.setattr(editable_pth, "repair_receipt_to_checkout", lambda _checkout: False)

        ok, message = _run(repair=True)

        assert ok is False
        assert "FAIL" in message

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


class TestExpectedCheckout:
    def test_returns_resolved_repo_when_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "clone"
        repo.mkdir()
        monkeypatch.setenv("T3_REPO", str(repo))
        assert expected_checkout() == repo.resolve()

    def test_returns_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_REPO", raising=False)
        assert expected_checkout() is None

    def test_returns_none_when_repo_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_REPO", str(tmp_path / "does-not-exist"))
        assert expected_checkout() is None


class TestRepairReceiptToCheckout:
    def test_returns_false_when_uv_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _tool: None)
        assert repair_receipt_to_checkout(tmp_path) is False

    def test_runs_force_editable_install_and_reports_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def _fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("shutil.which", lambda _tool: "/usr/bin/uv")
        monkeypatch.setattr("subprocess.run", _fake_run)

        assert repair_receipt_to_checkout(tmp_path) is True
        assert calls == [["/usr/bin/uv", "tool", "install", "--editable", str(tmp_path), "--force"]]

    def test_returns_false_on_nonzero_exit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="boom")

        monkeypatch.setattr("shutil.which", lambda _tool: "/usr/bin/uv")
        monkeypatch.setattr("subprocess.run", _fake_run)

        assert repair_receipt_to_checkout(tmp_path) is False

    def test_returns_false_when_subprocess_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_argv: list[str], **_kwargs: object) -> object:
            msg = "uv missing"
            raise OSError(msg)

        monkeypatch.setattr("shutil.which", lambda _tool: "/usr/bin/uv")
        monkeypatch.setattr("subprocess.run", _boom)

        assert repair_receipt_to_checkout(tmp_path) is False
