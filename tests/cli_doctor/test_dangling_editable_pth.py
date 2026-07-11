# test-path: cross-cutting
"""``_check_dangling_editable_pth`` + the editable-.pth detection primitive.

The reaped-worktree footgun: a sub-agent repointed the GLOBAL uv-tool
``teatree.pth`` at its own worktree, ``clean-all`` reaped that worktree, and the
dangling ``.pth`` then killed ``t3`` machine-wide with ``ModuleNotFoundError: No
module named 't3_bootstrap'``. These drive the detection + safe-repair against a
real on-disk uv-tool layout under ``tmp_path``.

Cross-cutting: the doctor check lives in ``teatree.cli`` while the detection
primitive lives in ``teatree.utils.editable_pth``, so no single mirror dir
covers both imports.
"""

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from teatree.cli.doctor.checks import _check_dangling_editable_pth
from teatree.utils import editable_pth


def _make_tool_layout(tmp_path: Path, *, pth_src: Path | None, editable: Path | None) -> Path:
    """Build a uv-tool dir with a teatree.pth (optional) and a receipt (optional)."""
    tool_dir = tmp_path / "uvtools"
    site = tool_dir / "teatree" / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)
    if pth_src is not None:
        (site / "teatree.pth").write_text(str(pth_src) + "\n", encoding="utf-8")
    if editable is not None:
        (tool_dir / "teatree" / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "teatree", editable = "' + str(editable) + '" }]\n',
            encoding="utf-8",
        )
    return tool_dir


class TestDetectDanglingEditable:
    def test_healthy_pth_is_not_dangling(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        live_src = tmp_path / "clone" / "src"
        live_src.mkdir(parents=True)
        tool_dir = _make_tool_layout(tmp_path, pth_src=live_src, editable=tmp_path / "clone")
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))

        dangling = editable_pth.detect_dangling_editable()

        assert dangling.is_dangling is False
        assert dangling.pth_dangling_dir is None
        assert dangling.receipt_source is None

    def test_pth_pointing_at_reaped_worktree_is_dangling(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        reaped = tmp_path / "teatree-wt-eval-ci-reuse" / "src"  # never created → dangling
        tool_dir = _make_tool_layout(tmp_path, pth_src=reaped, editable=None)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))

        dangling = editable_pth.detect_dangling_editable()

        assert dangling.is_dangling is True
        assert dangling.pth_dangling_dir == reaped

    def test_receipt_pointing_at_gone_clone_is_dangling(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        live_src = tmp_path / "clone" / "src"
        live_src.mkdir(parents=True)
        gone_clone = tmp_path / "reaped-clone"  # never created
        tool_dir = _make_tool_layout(tmp_path, pth_src=live_src, editable=gone_clone)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))

        dangling = editable_pth.detect_dangling_editable()

        assert dangling.is_dangling is True
        assert dangling.receipt_source == gone_clone
        assert dangling.pth_dangling_dir is None

    def test_no_tool_install_is_not_dangling(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "absent"))
        dangling = editable_pth.detect_dangling_editable()
        assert dangling.is_dangling is False
        assert dangling.pth is None

    def test_pth_skips_comment_and_import_lines(self, tmp_path: Path) -> None:
        pth = tmp_path / "teatree.pth"
        src = tmp_path / "clone" / "src"
        src.mkdir(parents=True)
        pth.write_text(f"# comment\nimport sys\n{src}\n", encoding="utf-8")
        assert editable_pth.pth_source_dirs(pth) == [src]


class TestRepairPthToCanonical:
    def test_rewrites_dangling_pth_to_canonical(self, tmp_path: Path) -> None:
        pth = tmp_path / "teatree.pth"
        pth.write_text(str(tmp_path / "reaped" / "src") + "\n", encoding="utf-8")
        canonical = tmp_path / "canonical" / "src"
        canonical.mkdir(parents=True)

        changed = editable_pth.repair_pth_to_canonical(pth, canonical)

        assert changed is True
        assert editable_pth.pth_source_dirs(pth) == [canonical]

    def test_noop_when_already_canonical(self, tmp_path: Path) -> None:
        canonical = tmp_path / "canonical" / "src"
        canonical.mkdir(parents=True)
        pth = tmp_path / "teatree.pth"
        pth.write_text(str(canonical) + "\n", encoding="utf-8")

        assert editable_pth.repair_pth_to_canonical(pth, canonical) is False

    def test_preserves_comment_and_import_lines(self, tmp_path: Path) -> None:
        # An editable .pth often carries an `import` directive (uv/setuptools
        # editable installs do) and/or a comment. Repairing the dangling path
        # entry must keep those lines in place — only the path line is rewritten.
        pth = tmp_path / "teatree.pth"
        pth.write_text(
            "# editable install\nimport sys; sys.path.insert(0, 'x')\n" + str(tmp_path / "reaped" / "src") + "\n",
            encoding="utf-8",
        )
        canonical = tmp_path / "canonical" / "src"
        canonical.mkdir(parents=True)

        changed = editable_pth.repair_pth_to_canonical(pth, canonical)

        assert changed is True
        # The dangling path entry is now the canonical src...
        assert editable_pth.pth_source_dirs(pth) == [canonical]
        # ...and the comment + import lines survived verbatim.
        contents = pth.read_text(encoding="utf-8")
        assert "# editable install" in contents
        assert "import sys; sys.path.insert(0, 'x')" in contents

    def test_collapses_multiple_path_entries_to_canonical(self, tmp_path: Path) -> None:
        # Two dangling path entries collapse to the single canonical src while a
        # comment between them is preserved.
        pth = tmp_path / "teatree.pth"
        pth.write_text(
            str(tmp_path / "reaped-a" / "src") + "\n# keep me\n" + str(tmp_path / "reaped-b" / "src") + "\n",
            encoding="utf-8",
        )
        canonical = tmp_path / "canonical" / "src"
        canonical.mkdir(parents=True)

        assert editable_pth.repair_pth_to_canonical(pth, canonical) is True
        assert editable_pth.pth_source_dirs(pth) == [canonical]
        assert "# keep me" in pth.read_text(encoding="utf-8")


class TestEditablePthPrimitives:
    def test_uv_tool_dir_default_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        assert editable_pth.uv_tool_dir() == Path.home() / ".local" / "share" / "uv" / "tools"

    def test_uv_tool_dir_honours_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "custom"))
        assert editable_pth.uv_tool_dir() == tmp_path / "custom"

    def test_teatree_pth_path_none_when_no_pth_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # lib/python*/site-packages exists but holds no teatree.pth.
        site = tmp_path / "uvtools" / "teatree" / "lib" / "python3.13" / "site-packages"
        site.mkdir(parents=True)
        monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "uvtools"))
        assert editable_pth.teatree_pth_path() is None

    def test_pth_source_dirs_empty_on_unreadable_file(self, tmp_path: Path) -> None:
        # A directory (not a file) read as text raises OSError → empty list.
        not_a_file = tmp_path / "as-dir"
        not_a_file.mkdir()
        assert editable_pth.pth_source_dirs(not_a_file) == []

    def test_receipt_source_none_on_unparsable_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        receipt = tmp_path / "uvtools" / "teatree" / "uv-receipt.toml"
        receipt.parent.mkdir(parents=True)
        receipt.write_text("this is not = valid toml [[[", encoding="utf-8")
        monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "uvtools"))
        assert editable_pth.receipt_editable_source() is None

    def test_receipt_source_none_when_non_editable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        receipt = tmp_path / "uvtools" / "teatree" / "uv-receipt.toml"
        receipt.parent.mkdir(parents=True)
        receipt.write_text('[tool]\nrequirements = [{ name = "teatree" }]\n', encoding="utf-8")
        monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "uvtools"))
        assert editable_pth.receipt_editable_source() is None

    def test_repair_pth_returns_false_on_write_error(self, tmp_path: Path) -> None:
        # The .pth path is a directory → write_text raises OSError → no repair claimed.
        pth_as_dir = tmp_path / "teatree.pth"
        pth_as_dir.mkdir()
        canonical = tmp_path / "canonical" / "src"
        canonical.mkdir(parents=True)
        assert editable_pth.repair_pth_to_canonical(pth_as_dir, canonical) is False

    def test_canonical_src_dir_none_when_t3_repo_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_REPO", raising=False)
        assert editable_pth.canonical_src_dir() is None

    def test_canonical_src_dir_none_when_src_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_REPO", str(tmp_path / "clone-without-src"))
        assert editable_pth.canonical_src_dir() is None

    def test_running_from_canonical_clone_false_when_no_canonical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_REPO", raising=False)
        assert editable_pth.running_from_canonical_clone() is False

    def test_running_from_canonical_clone_true_when_matching(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point T3_REPO at the actual running teatree's clone so the resolved
        # ``$T3_REPO/src`` equals the running package's parent-of-parent.
        import teatree  # noqa: PLC0415

        running_src = Path(teatree.__file__).resolve().parent.parent
        monkeypatch.setenv("T3_REPO", str(running_src.parent))
        assert editable_pth.running_from_canonical_clone() is True

    def test_running_from_canonical_clone_false_on_resolution_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A canonical exists, but the running-package resolution raises → fail safe.
        canonical_clone = tmp_path / "canonical"
        (canonical_clone / "src").mkdir(parents=True)
        monkeypatch.setenv("T3_REPO", str(canonical_clone))
        import teatree  # noqa: PLC0415

        monkeypatch.setattr(teatree, "__file__", None)
        # __file__ None → "" → Path("").resolve() is cwd; force the KeyError path
        # by removing the module from sys.modules lookup instead.
        monkeypatch.setattr(editable_pth, "canonical_src_dir", lambda: canonical_clone / "src")
        monkeypatch.delitem(editable_pth.sys.modules, "teatree", raising=False)
        assert editable_pth.running_from_canonical_clone() is False


def _run_check() -> tuple[bool, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        ok = _check_dangling_editable_pth()
    return ok, out.getvalue()


class TestCheckDanglingEditablePth:
    def test_passes_on_healthy_install(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        live_src = tmp_path / "clone" / "src"
        live_src.mkdir(parents=True)
        tool_dir = _make_tool_layout(tmp_path, pth_src=live_src, editable=tmp_path / "clone")
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))

        ok, message = _run_check()

        assert ok is True
        assert "FAIL" not in message

    def test_fails_and_reports_dangling_pth_when_repair_unsafe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Repair is unsafe: the running t3 is not importing from $T3_REPO/src
        # (no T3_REPO set), so the dangling .pth is reported, never silently rewritten.
        reaped = tmp_path / "reaped-wt" / "src"
        tool_dir = _make_tool_layout(tmp_path, pth_src=reaped, editable=None)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.delenv("T3_REPO", raising=False)

        ok, message = _run_check()

        assert ok is False
        assert "FAIL" in message
        assert str(reaped) in message
        # The .pth was NOT rewritten — repair is gated on running from the canonical clone.
        pth = tool_dir / "teatree" / "lib" / "python3.13" / "site-packages" / "teatree.pth"
        assert editable_pth.pth_source_dirs(pth) == [reaped]

    def test_auto_repairs_when_running_from_canonical_clone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        canonical_clone = tmp_path / "canonical"
        canonical_src = canonical_clone / "src"
        canonical_src.mkdir(parents=True)
        reaped = tmp_path / "reaped-wt" / "src"
        tool_dir = _make_tool_layout(tmp_path, pth_src=reaped, editable=None)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.setenv("T3_REPO", str(canonical_clone))
        # Simulate the running t3 importing teatree from the canonical clone.
        monkeypatch.setattr(editable_pth, "running_from_canonical_clone", lambda: True)

        ok, message = _run_check()

        # The repair healed the only problem → the check passes (a WARN, not a
        # FAIL). The stale "Re-anchor: re-run t3 setup" FAIL must NOT print for a
        # .pth this run just repaired.
        assert ok is True
        assert "Repaired dangling teatree editable .pth" in message
        assert "FAIL" not in message
        assert "Re-anchor" not in message
        pth = tool_dir / "teatree" / "lib" / "python3.13" / "site-packages" / "teatree.pth"
        assert editable_pth.pth_source_dirs(pth) == [canonical_src]

    def test_repaired_pth_with_dangling_receipt_suppresses_stale_pth_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # BOTH the .pth and the uv receipt are dangling. The .pth repair succeeds,
        # but the receipt clone is still gone. The healed .pth must NOT FAIL (no
        # "Re-anchor" message), while the genuinely-unrelated receipt FAIL is kept.
        canonical_clone = tmp_path / "canonical"
        canonical_src = canonical_clone / "src"
        canonical_src.mkdir(parents=True)
        reaped = tmp_path / "reaped-wt" / "src"  # dangling .pth target
        gone_clone = tmp_path / "reaped-clone"  # dangling receipt source
        tool_dir = _make_tool_layout(tmp_path, pth_src=reaped, editable=gone_clone)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
        monkeypatch.setenv("T3_REPO", str(canonical_clone))
        monkeypatch.setattr(editable_pth, "running_from_canonical_clone", lambda: True)

        ok, message = _run_check()

        assert ok is False  # the receipt is still broken
        assert "Repaired dangling teatree editable .pth" in message
        # The .pth FAIL / re-anchor advice is suppressed — that link was just
        # healed. The reaped path may still appear in the WARN ("was <reaped>"),
        # but no FAIL line should mention it.
        assert "Re-anchor" not in message
        assert "teatree editable .pth points at a non-existent dir" not in message
        fail_lines = [line for line in message.splitlines() if line.startswith("FAIL")]
        assert all(str(reaped) not in line for line in fail_lines)
        # The genuinely-unrelated receipt FAIL is preserved and accurate.
        assert "uv tool receipt records a non-existent editable source" in message
        assert str(gone_clone) in message
        # The .pth on disk really was repaired to the canonical src.
        pth = tool_dir / "teatree" / "lib" / "python3.13" / "site-packages" / "teatree.pth"
        assert editable_pth.pth_source_dirs(pth) == [canonical_src]

    def test_crash_proof_degrades_to_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An unexpected error in detection must WARN and pass, never abort doctor.
        def _boom() -> editable_pth.DanglingEditable:
            msg = "disk on fire"
            raise RuntimeError(msg)

        monkeypatch.setattr("teatree.utils.editable_pth.detect_dangling_editable", _boom)
        ok, message = _run_check()
        assert ok is True
        assert "WARN" in message
        assert "Could not inspect" in message

    def test_reports_dangling_receipt_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        live_src = tmp_path / "clone" / "src"
        live_src.mkdir(parents=True)
        gone_clone = tmp_path / "reaped-clone"
        tool_dir = _make_tool_layout(tmp_path, pth_src=live_src, editable=gone_clone)
        monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))

        ok, message = _run_check()

        assert ok is False
        assert "uv tool receipt records a non-existent editable source" in message
        assert str(gone_clone) in message
