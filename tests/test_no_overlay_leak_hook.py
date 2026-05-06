"""Tests for the no-overlay-leak gate (BLUEPRINT § 1, phase 8).

These tests run the hook script in a tmp_path with a fake `src/teatree/`
tree to assert it catches every banned term and ignores false positives.
"""

import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "hooks" / "check_no_overlay_leak.py"


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _seed(root: Path, relpath: str, content: str) -> Path:
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


class TestNoOverlayLeakHook:
    def test_passes_on_clean_tree(self, tmp_path: Path) -> None:
        _seed(tmp_path, "src/teatree/foo.py", "def foo() -> None:\n    pass\n")
        _seed(tmp_path, "docs/README.md", "# TeaTree\n\nGeneric docs.\n")

        result = _run(tmp_path)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_blocks_overlay_name_in_src(self, tmp_path: Path) -> None:
        _seed(tmp_path, "src/teatree/foo.py", '"""See t3-oper for details."""\n')

        result = _run(tmp_path)

        assert result.returncode == 1
        assert "t3-oper" in result.stdout

    def test_blocks_customer_name_in_docs(self, tmp_path: Path) -> None:
        _seed(tmp_path, "docs/integrations.md", "# Finporta\n\nIntegration notes.\n")

        result = _run(tmp_path)

        assert result.returncode == 1
        assert "finporta" in result.stdout.lower()

    def test_ignores_substring_matches(self, tmp_path: Path) -> None:
        _seed(
            tmp_path,
            "src/teatree/foo.py",
            "Operations and operators are fine. Cooperative tasks too.\n",
        )

        result = _run(tmp_path)

        assert result.returncode == 0, result.stdout

    def test_ignores_files_outside_scan_roots(self, tmp_path: Path) -> None:
        _seed(tmp_path, "overlays/t3-oper/README.md", "# t3-oper overlay\n")
        _seed(tmp_path, "tests/test_oper.py", "# t3-oper integration tests\n")

        result = _run(tmp_path)

        assert result.returncode == 0, result.stdout

    def test_ignores_non_text_suffixes(self, tmp_path: Path) -> None:
        _seed(tmp_path, "src/teatree/static/img.bin", "t3-oper bytes here\n")

        result = _run(tmp_path)

        assert result.returncode == 0, result.stdout

    @pytest.mark.parametrize(
        "term",
        [
            "t3-oper",
            "oper-product",
            "finporta",
            "atruvia",
            "wuestenrot",
            "home-savings",
            "sparkasse",
            "goerlich",
            "atplaywright",
        ],
    )
    def test_each_banned_term_is_caught(self, tmp_path: Path, term: str) -> None:
        _seed(tmp_path, "src/teatree/foo.py", f"# Reference to {term}\n")

        result = _run(tmp_path)

        assert result.returncode == 1
        assert term.lower() in result.stdout.lower()

    def test_case_insensitive(self, tmp_path: Path) -> None:
        _seed(tmp_path, "src/teatree/foo.py", "# FINPORTA reference\n")

        result = _run(tmp_path)

        assert result.returncode == 1

    @pytest.mark.parametrize(
        "snake_variant",
        ["home_savings", "oper_product", "oper_skills", "t3_oper", "t3_oper_e2e"],
    )
    def test_blocks_snake_case_variant(self, tmp_path: Path, snake_variant: str) -> None:
        _seed(tmp_path, "src/teatree/foo.py", f"{snake_variant} = True\n")

        result = _run(tmp_path)

        assert result.returncode == 1
        assert snake_variant in result.stdout.lower()

    @pytest.mark.parametrize(
        "camel_variant",
        ["homeSavings", "operProduct", "t3Oper"],
    )
    def test_blocks_camel_case_variant(self, tmp_path: Path, camel_variant: str) -> None:
        _seed(tmp_path, "src/teatree/foo.py", f"value = {camel_variant}\n")

        result = _run(tmp_path)

        assert result.returncode == 1
        assert camel_variant.lower() in result.stdout.lower()

    @pytest.mark.parametrize(
        "pascal_variant",
        ["HomeSavings", "OperProduct", "T3Oper"],
    )
    def test_blocks_pascal_case_variant(self, tmp_path: Path, pascal_variant: str) -> None:
        _seed(tmp_path, "src/teatree/foo.py", f"class {pascal_variant}: pass\n")

        result = _run(tmp_path)

        assert result.returncode == 1
        assert pascal_variant.lower() in result.stdout.lower()
