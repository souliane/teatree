"""Tests for the no-overlay-leak gate (BLUEPRINT § 1).

The hook loads forbidden tokens at runtime from
``$TEATREE_OVERLAY_LEAK_TERMS`` (comma-separated). These tests inject a
small set of placeholder tokens via that env var and assert the hook
catches them and ignores false positives.

The gate uses the SAME whole-token matcher as the ``banned_terms`` posting
gate (``teatree.hooks.term_match``): a term matches only when its own tokens
appear as a contiguous run of whole tokens, with ``-``, ``_``, whitespace,
punctuation AND camelCase/PascalCase boundaries as separators. So a glued
camelCase/PascalCase identifier (``demoSavings`` / ``DemoSavings``) is split
to ``[demo, savings]`` and DOES trip a configured ``demo-savings`` term, and
a fully-lowercase glued spelling (``demosavings``) is caught too via the
multi-token glued fallback. A clean identifier with no embedded term
(``getUserName``) is unaffected.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "hooks" / "check_no_overlay_leak.py"

# Placeholder tokens used only for testing the matching mechanism.
# The real forbidden list is loaded from the operator's local config
# at runtime — never committed to this repo.
FAKE_TERMS = (
    "t3-fake-overlay",
    "fake-product",
    "fake-skills",
    "alpha-tenant",
    "beta-tenant",
    "demo-tenant",
    "stub-name",
    "stub-platform",
    "demo-savings",
)
TERMS_ENV = ",".join(FAKE_TERMS)


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "TEATREE_OVERLAY_LEAK_TERMS": TERMS_ENV}
    return subprocess.run(
        [sys.executable, str(HOOK), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
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
        _seed(tmp_path, "src/teatree/foo.py", '"""See t3-fake-overlay for details."""\n')

        result = _run(tmp_path)

        assert result.returncode == 1
        assert "t3-fake-overlay" in result.stdout

    def test_blocks_tenant_name_in_docs(self, tmp_path: Path) -> None:
        _seed(tmp_path, "docs/integrations.md", "# Alpha-Tenant\n\nIntegration notes.\n")

        result = _run(tmp_path)

        assert result.returncode == 1
        assert "alpha-tenant" in result.stdout.lower()

    def test_ignores_substring_matches(self, tmp_path: Path) -> None:
        _seed(
            tmp_path,
            "src/teatree/foo.py",
            "Operations and operators are fine. Cooperative tasks too.\n",
        )

        result = _run(tmp_path)

        assert result.returncode == 0, result.stdout

    def test_ignores_files_outside_scan_roots(self, tmp_path: Path) -> None:
        _seed(tmp_path, "overlays/t3-fake-overlay/README.md", "# t3-fake-overlay overlay\n")
        _seed(tmp_path, "tests/test_fake.py", "# t3-fake-overlay integration tests\n")

        result = _run(tmp_path)

        assert result.returncode == 0, result.stdout

    def test_ignores_non_text_suffixes(self, tmp_path: Path) -> None:
        _seed(tmp_path, "src/teatree/static/img.bin", "t3-fake-overlay bytes here\n")

        result = _run(tmp_path)

        assert result.returncode == 0, result.stdout

    def test_passes_when_no_terms_configured(self, tmp_path: Path) -> None:
        _seed(tmp_path, "src/teatree/foo.py", "anything goes here\n")

        env = {k: v for k, v in os.environ.items() if k != "TEATREE_OVERLAY_LEAK_TERMS"}
        env.setdefault("HOME", str(tmp_path))  # avoid reading the operator's real config
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("term", FAKE_TERMS)
    def test_each_configured_term_is_caught(self, tmp_path: Path, term: str) -> None:
        _seed(tmp_path, "src/teatree/foo.py", f"# Reference to {term}\n")

        result = _run(tmp_path)

        assert result.returncode == 1
        assert term.lower() in result.stdout.lower()

    def test_case_insensitive(self, tmp_path: Path) -> None:
        _seed(tmp_path, "src/teatree/foo.py", "# ALPHA-TENANT reference\n")

        result = _run(tmp_path)

        assert result.returncode == 1

    @pytest.mark.parametrize(
        "snake_variant",
        ["demo_savings", "fake_product", "fake_skills", "t3_fake_overlay"],
    )
    def test_blocks_snake_case_variant(self, tmp_path: Path, snake_variant: str) -> None:
        _seed(tmp_path, "src/teatree/foo.py", f"{snake_variant} = True\n")

        result = _run(tmp_path)

        assert result.returncode == 1
        assert snake_variant in result.stdout.lower()

    def test_blocks_space_separated_variant(self, tmp_path: Path) -> None:
        # Whitespace is a token separator too, so a multi-word term written
        # with spaces tokenizes identically to its kebab form and is caught.
        _seed(tmp_path, "docs/notes.md", "# Notes\n\nthe demo savings flow\n")

        result = _run(tmp_path)

        assert result.returncode == 1
        assert "demo-savings" in result.stdout.lower()

    @pytest.mark.parametrize(
        "glued_variant",
        # camelCase/PascalCase boundaries are token separators, so a glued
        # multi-word-term identifier is split back to its tokens and caught.
        ["demoSavings", "fakeProduct", "t3FakeOverlay", "DemoSavings", "FakeProduct", "T3FakeOverlay"],
    )
    def test_glued_camel_or_pascal_multiword_variant_is_blocked(self, tmp_path: Path, glued_variant: str) -> None:
        _seed(tmp_path, "src/teatree/foo.py", f"value = {glued_variant}\n")

        result = _run(tmp_path)

        assert result.returncode == 1, result.stdout

    @pytest.mark.parametrize("lowercase_glued", ["demosavings", "fakeproduct"])
    def test_lowercase_glued_multiword_variant_is_blocked(self, tmp_path: Path, lowercase_glued: str) -> None:
        # A fully-lowercase glued spelling has no camelCase boundary, but the
        # multi-token glued fallback still catches it for a multi-word term.
        _seed(tmp_path, "src/teatree/foo.py", f"value = {lowercase_glued}\n")

        result = _run(tmp_path)

        assert result.returncode == 1, result.stdout

    @pytest.mark.parametrize("camel_variant", ["acmeProduct", "AcmeProduct", "useAcmeClient"])
    def test_single_word_term_embedded_in_camelcase_is_blocked(self, tmp_path: Path, camel_variant: str) -> None:
        # A single-word term (here the synthetic ``acme``) is matched once a
        # camelCase identifier splits it out as its own whole token.
        env = {**os.environ, "TEATREE_OVERLAY_LEAK_TERMS": "acme"}
        _seed(tmp_path, "src/teatree/foo.py", f"value = {camel_variant}\n")

        result = subprocess.run(
            [sys.executable, str(HOOK)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        assert result.returncode == 1, result.stdout
        assert "acme" in result.stdout.lower()

    @pytest.mark.parametrize("clean_word", ["operator", "operation", "cooperation", "operate"])
    def test_operator_style_single_word_is_not_blocked(self, tmp_path: Path, clean_word: str) -> None:
        # A short single-word term (synthetic ``op``) must not surface inside a
        # longer unbroken word — the operator-class false positive stays clean.
        env = {**os.environ, "TEATREE_OVERLAY_LEAK_TERMS": "op"}
        _seed(tmp_path, "src/teatree/foo.py", f"# notes about {clean_word}\n")

        result = subprocess.run(
            [sys.executable, str(HOOK)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        assert result.returncode == 0, result.stdout

    def test_clean_camelcase_identifier_with_no_term_is_not_blocked(self, tmp_path: Path) -> None:
        # ``getUserName`` splits to [get, user, name]; none is a configured term.
        _seed(tmp_path, "src/teatree/foo.py", "value = getUserName()\n")

        result = _run(tmp_path)

        assert result.returncode == 0, result.stdout
