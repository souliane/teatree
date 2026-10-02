"""Tests for the term-source drift detector (``scripts/term_source_drift.py``).

The detector answers one question for each CI-fed leak gate: *is the term list
the gate actually scans the list the operator configured?* It compares three
places a list can live — the consolidated ``banned_term_registry`` class, the
legacy ``ConfigSetting`` row, and the CI secret CI injects via env — and reports
only COUNTS and salted digests. A term VALUE must never reach stdout, stderr, a
committed file, or a CI log, so the privacy assertions below are as load-bearing
as the drift ones.

The placeholder terms these tests use are invented fixture strings, never real
configured terms.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "term_source_drift.py"

#: Invented placeholder terms. They must not collide with anything real, and the
#: privacy assertions grep process output for them.
TREE_TERMS = ["zarquon-corp", "blorbtech", "quuxical"]
OVERLAY_TERMS = ["frobnitz-overlay", "wibble-tenant"]


def _seed_db(tmp_path: Path, settings: dict[str, object]) -> Path:
    """Build a ``teatree_config_setting`` DB carrying *settings* as JSON values."""
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE teatree_config_setting ("
        " id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '',"
        " key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    for key, value in settings.items():
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
            (key, json.dumps(value)),
        )
    conn.commit()
    conn.close()
    return db


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the detector with a clean environment plus *env*."""
    child = {k: v for k, v in os.environ.items() if not k.startswith("TEATREE_")}
    child.update(env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=child,
    )


def _assert_no_term_leaked(result: subprocess.CompletedProcess[str], terms: list[str]) -> None:
    """No configured term value may appear in the detector's output."""
    blob = f"{result.stdout}\n{result.stderr}".lower()
    for term in terms:
        assert term.lower() not in blob, f"the detector printed a configured term ({result.stdout}{result.stderr})"


@pytest.fixture
def fingerprint(tmp_path: Path) -> Path:
    """A fingerprint file generated from a DB whose registry covers both legacy rows."""
    db = _seed_db(
        tmp_path,
        {
            "banned_brands": TREE_TERMS,
            "overlay_leak_terms": OVERLAY_TERMS,
            "banned_term_registry": {"leak": TREE_TERMS, "overlay": OVERLAY_TERMS},
        },
    )
    out = tmp_path / "fingerprint.json"
    result = _run("generate", "--db", str(db), "--out", str(out))
    assert result.returncode == 0, result.stderr
    return out


class TestGenerate:
    def test_writes_counts_and_digests_but_no_terms(self, tmp_path: Path, fingerprint: Path) -> None:
        payload = json.loads(fingerprint.read_text(encoding="utf-8"))
        assert payload["gates"]["tree"]["count"] == len(TREE_TERMS)
        assert payload["gates"]["overlay"]["count"] == len(OVERLAY_TERMS)
        assert payload["gates"]["tree"]["digest"].startswith("sha256:")
        raw = fingerprint.read_text(encoding="utf-8").lower()
        for term in TREE_TERMS + OVERLAY_TERMS:
            assert term.lower() not in raw, "a term value reached the committed fingerprint file"

    def test_widest_source_wins_so_a_shadowing_registry_cannot_shrink_it(self, tmp_path: Path) -> None:
        """A registry class narrower than its legacy row must not shrink the fingerprint."""
        db = _seed_db(
            tmp_path,
            {
                "banned_brands": TREE_TERMS,
                "overlay_leak_terms": OVERLAY_TERMS,
                # The live shape this detector exists for: the registry omits the
                # overlay class entirely and carries only one of the three brands.
                "banned_term_registry": {"leak": TREE_TERMS[:1]},
            },
        )
        out = tmp_path / "fp.json"
        assert _run("generate", "--db", str(db), "--out", str(out)).returncode == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["gates"]["tree"]["count"] == len(TREE_TERMS)
        assert payload["gates"]["overlay"]["count"] == len(OVERLAY_TERMS)


class TestCheckCi:
    def test_matching_ci_list_passes(self, fingerprint: Path) -> None:
        result = _run(
            "check-ci",
            "--fingerprint",
            str(fingerprint),
            env={
                "TEATREE_BANNED_BRANDS": ",".join(TREE_TERMS),
                "TEATREE_OVERLAY_LEAK_TERMS": ",".join(OVERLAY_TERMS),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        _assert_no_term_leaked(result, TREE_TERMS + OVERLAY_TERMS)

    def test_stale_smaller_ci_list_fails_with_counts_only(self, fingerprint: Path) -> None:
        """The exact drift that went unnoticed: the secret holds an older, shorter list."""
        result = _run(
            "check-ci",
            "--fingerprint",
            str(fingerprint),
            env={
                "TEATREE_BANNED_BRANDS": ",".join(TREE_TERMS[:1]),
                "TEATREE_OVERLAY_LEAK_TERMS": ",".join(OVERLAY_TERMS),
            },
        )
        assert result.returncode == 1
        assert "tree" in result.stdout
        assert str(len(TREE_TERMS)) in result.stdout
        _assert_no_term_leaked(result, TREE_TERMS + OVERLAY_TERMS)

    def test_same_size_different_content_fails_on_the_digest(self, fingerprint: Path) -> None:
        swapped = [*TREE_TERMS[:-1], "different-term"]
        result = _run(
            "check-ci",
            "--fingerprint",
            str(fingerprint),
            env={
                "TEATREE_BANNED_BRANDS": ",".join(swapped),
                "TEATREE_OVERLAY_LEAK_TERMS": ",".join(OVERLAY_TERMS),
            },
        )
        assert result.returncode == 1
        assert "digest" in result.stdout.lower()

    def test_order_and_case_and_whitespace_do_not_count_as_drift(self, fingerprint: Path) -> None:
        noisy = ", ".join(term.upper() for term in reversed(TREE_TERMS))
        result = _run(
            "check-ci",
            "--fingerprint",
            str(fingerprint),
            env={
                "TEATREE_BANNED_BRANDS": noisy,
                "TEATREE_OVERLAY_LEAK_TERMS": ",".join(OVERLAY_TERMS),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_unset_secret_is_misconfigured_by_default(self, fingerprint: Path) -> None:
        result = _run("check-ci", "--fingerprint", str(fingerprint))
        assert result.returncode == 2
        assert "TEATREE_BANNED_BRANDS" in result.stdout

    def test_allow_unset_skips_for_a_fork_pr(self, fingerprint: Path) -> None:
        result = _run("check-ci", "--fingerprint", str(fingerprint), "--allow-unset")
        assert result.returncode == 0
        assert "skip" in result.stdout.lower()

    def test_malformed_fingerprint_raises_instead_of_passing_vacuously(self, tmp_path: Path) -> None:
        """An unreadable contract must fail, never read as an empty one every list satisfies."""
        broken = tmp_path / "broken.json"
        broken.write_text('{"gates": []}', encoding="utf-8")
        result = _run(
            "check-ci",
            "--fingerprint",
            str(broken),
            env={"TEATREE_BANNED_BRANDS": ",".join(TREE_TERMS)},
        )
        assert result.returncode != 0


class TestCheckShadow:
    def test_registry_covering_every_legacy_row_passes(self, tmp_path: Path) -> None:
        db = _seed_db(
            tmp_path,
            {
                "banned_brands": TREE_TERMS,
                "overlay_leak_terms": OVERLAY_TERMS,
                "banned_term_registry": {"leak": TREE_TERMS, "overlay": OVERLAY_TERMS},
            },
        )
        result = _run("check-shadow", "--db", str(db))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_registry_class_narrower_than_its_legacy_row_is_reported(self, tmp_path: Path) -> None:
        """Registry-first resolution silently shrinks a gate when a class lags its row."""
        db = _seed_db(
            tmp_path,
            {
                "banned_brands": TREE_TERMS,
                "overlay_leak_terms": OVERLAY_TERMS,
                "banned_term_registry": {"leak": TREE_TERMS[:1]},
            },
        )
        result = _run("check-shadow", "--db", str(db))
        assert result.returncode == 1
        assert "tree" in result.stdout
        assert "overlay" in result.stdout
        _assert_no_term_leaked(result, TREE_TERMS + OVERLAY_TERMS)

    def test_absent_registry_is_not_shadowing(self, tmp_path: Path) -> None:
        """Pre-cutover the registry is unset and every gate reads its legacy row."""
        db = _seed_db(tmp_path, {"banned_brands": TREE_TERMS, "overlay_leak_terms": OVERLAY_TERMS})
        result = _run("check-shadow", "--db", str(db))
        assert result.returncode == 0, result.stdout + result.stderr
