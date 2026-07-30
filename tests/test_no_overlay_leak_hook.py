"""Tests for the no-overlay-leak gate (BLUEPRINT § 1).

The hook loads forbidden tokens at runtime from
``$TEATREE_OVERLAY_LEAK_TERMS`` (comma-separated), else the DB-home
``overlay_leak_terms`` ``ConfigSetting`` row. These tests inject a small set of
placeholder tokens via that env var (and a seeded DB) and assert the hook
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

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "hooks" / "check_no_overlay_leak.py"


def _seed_overlay_leak_db(tmp_path: Path, terms: list[str]) -> Path:
    """Build a ``teatree_config_setting`` DB carrying the ``overlay_leak_terms`` row."""
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
        "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'overlay_leak_terms', ?)",
        (json.dumps(terms),),
    )
    conn.commit()
    conn.close()
    return db


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
        # ``overlays/`` is where overlay-specific names BELONG — it is not a
        # scanned root. (``tests/`` IS a scanned root since fix #6, so a leak
        # there is correctly caught — covered in TestExpandedScanRoots.)
        _seed(tmp_path, "overlays/t3-fake-overlay/README.md", "# t3-fake-overlay overlay\n")
        _seed(tmp_path, "frontend/app.js", "// t3-fake-overlay frontend\n")

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


def _run_no_terms(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the hook with NO terms configured (env unset, HOME isolated)."""
    env = {k: v for k, v in os.environ.items() if k not in {"TEATREE_OVERLAY_LEAK_TERMS", "T3_CONFIG_DB"}}
    env["HOME"] = str(cwd)  # isolate HOME so no host config DB is resolved
    return subprocess.run(
        [sys.executable, str(HOOK), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


class TestRequireTermsFlag:
    """Fix #2: ``--require-terms`` makes an UNSET term list a LOUD failure.

    The gate is silently inert when neither ``TEATREE_OVERLAY_LEAK_TERMS``
    nor the ``overlay_leak_terms`` DB row is populated — a real leak sits
    unguarded and the job stays green. ``--require-terms`` (the form CI passes)
    turns that misconfiguration into exit 2; local dev omits the flag and stays
    green, mirroring the brand backstop's ``--require-brands``.
    """

    def test_require_terms_hard_fails_when_unset(self, tmp_path: Path) -> None:
        _seed(tmp_path, "src/teatree/foo.py", "clean = True\n")
        result = _run_no_terms(tmp_path, "--require-terms")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "MISCONFIGURED" in result.stdout

    def test_without_flag_unset_terms_stays_green(self, tmp_path: Path) -> None:
        # Anti-vacuity: the SAME unset-terms tree that exits 2 under
        # --require-terms exits 0 without it (with a loud inert warning).
        _seed(tmp_path, "src/teatree/foo.py", "clean = True\n")
        result = _run_no_terms(tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "INERT" in result.stdout

    def test_require_terms_with_terms_configured_runs_normally(self, tmp_path: Path) -> None:
        # The flag only hard-fails on the unset state; a populated term list
        # scans normally — a clean tree exits 0 even with the flag.
        _seed(tmp_path, "src/teatree/foo.py", "clean = True\n")
        result = _run(tmp_path, "--require-terms")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "MISCONFIGURED" not in result.stdout

    def test_require_terms_with_terms_still_reports_findings_as_exit_1(self, tmp_path: Path) -> None:
        # A dirty tree under --require-terms is exit 1 (findings), NOT exit 2 —
        # the two failure modes stay distinct.
        _seed(tmp_path, "src/teatree/foo.py", '"""See t3-fake-overlay."""\n')
        result = _run(tmp_path, "--require-terms")
        assert result.returncode == 1
        assert "t3-fake-overlay" in result.stdout


class TestExpandedScanRoots:
    """Fix #6: the scan covers every root where real leaks lived.

    The old roots were only ``src/teatree`` and ``docs``. Real leaks lived
    in ``skills/``, ``agents/``, ``tests/``, ``scripts/`` and the top-level
    ``README.md`` / ``BLUEPRINT.md`` / ``AGENTS.md``. Each must now be scanned.
    """

    @pytest.mark.parametrize(
        "relpath",
        [
            "skills/some-skill/SKILL.md",
            "agents/planner.md",
            "tests/test_thing.py",
            "scripts/do_thing.py",
            "README.md",
            "BLUEPRINT.md",
            "AGENTS.md",
        ],
    )
    def test_term_in_expanded_root_is_caught(self, tmp_path: Path, relpath: str) -> None:
        _seed(tmp_path, relpath, "# reference to alpha-tenant here\n")
        result = _run(tmp_path)
        assert result.returncode == 1, result.stdout
        assert "alpha-tenant" in result.stdout.lower()


class TestOpaqueIdDetection:
    """Fix #3: Slack/forge opaque IDs are a NEW leak class on the full tree.

    A real-shaped channel/DM/user/app/team id (``C0…``/``D0…``/``U0…``/
    ``A0…``/``T0…``) is an internal reference with no dictionary word, so the
    term-list gate never caught it. A synthetic-placeholder allowlist keeps
    fixtures/examples (``C0DEMO*``, ``U01ABCD1234`` …) from tripping.
    """

    def test_real_shaped_slack_id_is_caught(self, tmp_path: Path) -> None:
        # Invented, random-looking id — not a real channel/DM/user id.
        _seed(tmp_path, "src/teatree/foo.py", "CHANNEL = 'C0ZX91QWERT'\n")
        result = _run(tmp_path)
        assert result.returncode == 1, result.stdout
        assert "C0ZX91QWERT" in result.stdout

    def test_real_shaped_id_caught_even_with_no_terms_configured(self, tmp_path: Path) -> None:
        # The opaque-ID pass is ALWAYS-ON — it does not need a configured term
        # list, unlike the overlay-leak terms. A real-shaped id trips even when
        # no terms are set (the gate is otherwise inert).
        _seed(tmp_path, "src/teatree/foo.py", "DM = 'D0KP47MNBVC'\n")
        result = _run_no_terms(tmp_path)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "D0KP47MNBVC" in result.stdout

    @pytest.mark.parametrize("placeholder", ["C0DEMOCHAN1", "U01ABCD1234", "D0CACHED", "U0AAAAAAAAA"])
    def test_synthetic_placeholder_id_is_not_caught(self, tmp_path: Path, placeholder: str) -> None:
        _seed(tmp_path, "src/teatree/foo.py", f"VALUE = '{placeholder}'\n")
        result = _run(tmp_path)
        assert result.returncode == 0, result.stdout

    def test_opaque_id_caught_in_expanded_root(self, tmp_path: Path) -> None:
        _seed(tmp_path, "skills/x/SKILL.md", "channel C0ZX91QWERT\n")
        result = _run(tmp_path)
        assert result.returncode == 1, result.stdout
        assert "C0ZX91QWERT" in result.stdout


class TestDbSourcedTerms:
    """The term list is DB-home: an ``overlay_leak_terms`` row drives the gate.

    The env override still WINS, but with no env the reader falls back to the
    canonical ``overlay_leak_terms`` ``ConfigSetting`` row (via
    ``teatree.config.cold_reader``). Tests seed a DB and point the subprocess at
    it with ``T3_CONFIG_DB``.
    """

    def _run_with_db(self, cwd: Path, db: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k != "TEATREE_OVERLAY_LEAK_TERMS"}
        env["HOME"] = str(cwd)
        env["T3_CONFIG_DB"] = str(db)
        return subprocess.run(
            [sys.executable, str(HOOK), *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_db_term_is_caught(self, tmp_path: Path) -> None:
        db = _seed_overlay_leak_db(tmp_path, ["alpha-tenant"])
        _seed(tmp_path, "src/teatree/foo.py", "# Reference to alpha-tenant\n")
        result = self._run_with_db(tmp_path, db, "--require-terms")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "alpha-tenant" in result.stdout.lower()

    def test_db_terms_populated_satisfies_require_terms(self, tmp_path: Path) -> None:
        db = _seed_overlay_leak_db(tmp_path, ["alpha-tenant"])
        _seed(tmp_path, "src/teatree/foo.py", "clean = True\n")
        result = self._run_with_db(tmp_path, db, "--require-terms")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "MISCONFIGURED" not in result.stdout

    def test_env_wins_over_db(self, tmp_path: Path) -> None:
        # When the env override is set, the DB list is NOT consulted: a file that
        # names ONLY a DB-only term (absent from the env list) is not flagged.
        db = _seed_overlay_leak_db(tmp_path, ["from-db-only"])
        _seed(tmp_path, "src/teatree/foo.py", "# only names from-db-only here\n")
        env = {**os.environ, "TEATREE_OVERLAY_LEAK_TERMS": "beta-tenant", "T3_CONFIG_DB": str(db)}
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert result.returncode == 0, result.stdout  # env list has no match; the DB list is ignored


def _seed_registry_db(tmp_path: Path, *, overlay: list[str], legacy: list[str] | None = None) -> Path:
    """Build a DB carrying the consolidated ``banned_term_registry`` (overlay class)."""
    db = tmp_path / "registry.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
        "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_term_registry', ?)",
        (json.dumps({"leak": ["democorp"], "overlay": overlay}),),
    )
    if legacy is not None:
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'overlay_leak_terms', ?)",
            (json.dumps(legacy),),
        )
    conn.commit()
    conn.close()
    return db


class TestRegistrySourcedTerms:
    """The consolidated registry's ``overlay`` class drives the gate, registry-first."""

    def _run_with_db(self, cwd: Path, db: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k != "TEATREE_OVERLAY_LEAK_TERMS"}
        env["HOME"] = str(cwd)
        env["T3_CONFIG_DB"] = str(db)
        return subprocess.run(
            [sys.executable, str(HOOK), *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_registry_overlay_term_is_caught(self, tmp_path: Path) -> None:
        db = _seed_registry_db(tmp_path, overlay=["alpha-tenant"])
        _seed(tmp_path, "src/teatree/foo.py", "# Reference to alpha-tenant\n")
        result = self._run_with_db(tmp_path, db, "--require-terms")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "alpha-tenant" in result.stdout.lower()

    def test_registry_wins_over_legacy_overlay_row(self, tmp_path: Path) -> None:
        # A present registry is authoritative: a file naming only the LEGACY-row term
        # (absent from the registry's overlay class) is not flagged.
        db = _seed_registry_db(tmp_path, overlay=["alpha-tenant"], legacy=["legacy-only"])
        _seed(tmp_path, "src/teatree/foo.py", "# only names legacy-only here\n")
        result = self._run_with_db(tmp_path, db)
        assert result.returncode == 0, result.stdout + result.stderr


class TestOverlayLeakCiPassesRequireTerms:
    """Fix #2 (CI side): the overlay-leak full-tree CI job passes ``--require-terms``."""

    def test_ci_step_runs_full_tree_scan_with_require_terms(self) -> None:
        import yaml  # noqa: PLC0415

        ci_path = Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml"
        ci = yaml.safe_load(ci_path.read_text())
        steps = ci["jobs"]["overlay-leak-tree"]["steps"]
        joined = " ".join(s.get("run", "") for s in steps if isinstance(s, dict))
        assert "check_no_overlay_leak.py" in joined, "The overlay-leak-tree CI step must run the full-tree scan."
        assert "--require-terms" in joined, (
            "The overlay-leak-tree CI step must pass --require-terms so a missing "
            "TEATREE_OVERLAY_LEAK_TERMS secret reds the job (fail-loud), not a silent no-op."
        )
