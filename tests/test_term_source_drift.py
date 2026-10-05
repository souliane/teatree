"""The CI drift checker fingerprints the classes in the single registry secret."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "term_source_drift.py"
REGISTRY = {"leak": ["zarquon-corp"], "prose_collider": ["widget"], "overlay": ["wibble-tenant"]}


def _seed(tmp_path: Path) -> Path:
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
            ("banned_term_registry", json.dumps(REGISTRY)),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _run(*args: str, secret: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "TEATREE_TERM_REGISTRY"}
    if secret is not None:
        env["TEATREE_TERM_REGISTRY"] = secret
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False, env=env)


@pytest.fixture
def fingerprint(tmp_path: Path) -> Path:
    db = _seed(tmp_path)
    target = tmp_path / "fingerprint.json"
    result = _run("generate", "--db", str(db), "--out", str(target))
    assert result.returncode == 0, result.stderr
    return target


def test_single_registry_secret_matches_fingerprints(fingerprint: Path) -> None:
    result = _run("check-ci", "--fingerprint", str(fingerprint), secret=json.dumps(REGISTRY))
    assert result.returncode == 0, result.stdout
    assert "zarquon-corp" not in result.stdout
    assert "wibble-tenant" not in result.stdout


def test_changed_class_is_reported_without_term_values(fingerprint: Path) -> None:
    secret = json.dumps({**REGISTRY, "leak": ["another-brand"]})
    result = _run("check-ci", "--fingerprint", str(fingerprint), secret=secret)
    assert result.returncode == 1
    assert "another-brand" not in result.stdout
    assert "DRIFT" in result.stdout


def test_missing_or_malformed_secret_fails_loud(fingerprint: Path) -> None:
    absent = _run("check-ci", "--fingerprint", str(fingerprint))
    malformed = _run("check-ci", "--fingerprint", str(fingerprint), secret="{broken")
    assert absent.returncode == 2
    assert malformed.returncode == 2
