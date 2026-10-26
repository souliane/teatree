# test-path: cross-cutting
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from teatree.utils.master_key import KeyFileSource
from teatree.utils.sealing import aad

READER = Path(__file__).resolve().parents[2] / "deploy" / "secret-read.sh"


def _fixture(tmp_path: Path, *, with_table: bool = True) -> tuple[Path, KeyFileSource]:
    data_dir = tmp_path / "teatree"
    data_dir.mkdir()
    db = data_dir / "db.sqlite3"
    source = KeyFileSource(tmp_path / "master.key")
    with closing(sqlite3.connect(db)) as connection:
        if with_table:
            connection.execute(
                "CREATE TABLE teatree_sealed_secret (id INTEGER PRIMARY KEY, name TEXT UNIQUE, key_id TEXT, "
                "nonce BLOB, ciphertext BLOB, created_at TEXT, updated_at TEXT)"
            )
            connection.commit()
    return db, source


def _run(db: Path, source: KeyFileSource, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "teatree.config.cold_secret_reader", *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "T3_CONFIG_DB": str(db), "T3_MASTER_KEY_SOURCE": source.label},
    )


def test_cold_reader_distinguishes_value_absence_and_unreadable(tmp_path: Path) -> None:
    db, source = _fixture(tmp_path)
    name = "anthropic/a/oauth"
    sealed = source.create().seal("first-line\nsecond-line", aad=aad("instance", name))
    with closing(sqlite3.connect(db)) as connection:
        connection.execute(
            "INSERT INTO teatree_sealed_secret (name, key_id, nonce, ciphertext) VALUES (?, ?, ?, ?)",
            (name, sealed.key_id, sealed.nonce, sealed.ciphertext),
        )
        connection.commit()
    answered = _run(db, source, "get", name)
    assert (answered.returncode, answered.stdout) == (0, "first-line\n")
    assert _run(db, source, "get", "anthropic/absent").returncode == 1
    assert _run(db, source, "has-prefix", "anthropic/").returncode == 0
    assert _run(db, source, "probe-prefix", "anthropic/").returncode == 0
    source.path.unlink()
    unreadable = _run(db, source, "get", name)
    assert unreadable.returncode == 2
    assert "first-line" not in unreadable.stderr + unreadable.stdout


def test_pre_migration_table_is_absent(tmp_path: Path) -> None:
    db, source = _fixture(tmp_path, with_table=False)
    assert _run(db, source, "get", "team/token").returncode == 1


def test_unopenable_database_is_unreadable(tmp_path: Path) -> None:
    db = tmp_path / "directory-not-a-database"
    db.mkdir()
    source = KeyFileSource(tmp_path / "master.key")
    assert _run(db, source, "get", "team/token").returncode == 2


def test_shell_selector_reads_sealed_rows_without_invoking_pass(tmp_path: Path) -> None:
    db, source = _fixture(tmp_path)
    name = "team/token"
    sealed = source.create().seal("sealed-value\nmetadata", aad=aad("instance", name))
    with closing(sqlite3.connect(db)) as connection:
        connection.execute(
            "INSERT INTO teatree_sealed_secret (name, key_id, nonce, ciphertext) VALUES (?, ?, ?, ?)",
            (name, sealed.key_id, sealed.nonce, sealed.ciphertext),
        )
        connection.commit()
    log = tmp_path / "pass-calls"
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = binary / "pass"
    fake.write_text('#!/bin/sh\necho "$*" >> "$PASS_LOG"\nprintf "pass-value\\n"\n')
    fake.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "PASS_LOG": str(log),
        "T3_CONFIG_DB": str(db),
        "T3_MASTER_KEY_SOURCE": source.label,
        "T3_SECRET_STORE": "sealed",
        "T3_SECRET_PYTHON": sys.executable,
    }
    answered = subprocess.run([str(READER), "get", name], env=environment, capture_output=True, text=True, check=False)
    absent = subprocess.run(
        [str(READER), "get", "team/absent"], env=environment, capture_output=True, text=True, check=False
    )
    host = subprocess.run([str(READER), "host-get", name], env=environment, capture_output=True, text=True, check=False)
    assert (answered.returncode, answered.stdout) == (0, "sealed-value\n")
    assert (absent.returncode, host.returncode) == (1, 1)
    assert not log.exists()
    environment["T3_SECRET_STORE"] = "pass"
    from_pass = subprocess.run([str(READER), "get", name], env=environment, capture_output=True, text=True, check=False)
    assert (from_pass.returncode, from_pass.stdout) == (0, "pass-value\n")
    assert log.read_text().splitlines() == [f"show {name}"]


@pytest.mark.parametrize("diagnostic", ["", "echo interpreter-failed >&2\n"])
def test_shell_interpreter_failure_is_unreadable_not_absent(tmp_path: Path, diagnostic: str) -> None:
    interpreter = tmp_path / "broken-python"
    interpreter.write_text(f"#!/bin/sh\n{diagnostic}exit 1\n")
    interpreter.chmod(0o755)
    result = subprocess.run(
        [str(READER), "get", "team/token"],
        env={**os.environ, "T3_SECRET_STORE": "sealed", "T3_SECRET_PYTHON": str(interpreter)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""


def _facade_run(db: Path, source: KeyFileSource, name: str) -> subprocess.CompletedProcess[str]:
    code = """
import sys
from teatree.utils.secrets import SecretNotFoundError, SecretStoreError, read_pass_required
try:
    value = read_pass_required(sys.argv[1])
except SecretNotFoundError:
    raise SystemExit(1)
except SecretStoreError:
    raise SystemExit(2)
sys.stdout.write(value + "\\n")
"""
    return subprocess.run(
        [sys.executable, "-c", code, name],
        env={
            **os.environ,
            "XDG_DATA_HOME": str(db.parent.parent),
            "T3_CONFIG_DB": str(db),
            "T3_SECRET_STORE": "sealed",
            "T3_MASTER_KEY_SOURCE": source.label,
            "DJANGO_SETTINGS_MODULE": "teatree.settings",
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("case", "expected_code", "expected_output"),
    [
        ("answered", 0, "first-line\n"),
        ("absent", 1, ""),
        ("empty", 1, ""),
        ("tampered", 2, ""),
        ("wrong-key", 2, ""),
        ("missing-key", 2, ""),
        ("missing-table", 1, ""),
        ("missing-db", 1, ""),
    ],
)
def test_shell_and_python_cold_readers_share_the_result_matrix(
    tmp_path: Path, case: str, expected_code: int, expected_output: str
) -> None:
    db, source = _fixture(tmp_path, with_table=case != "missing-table")
    name = "team/token"
    if case not in {"absent", "missing-table", "missing-db"}:
        value = "" if case == "empty" else "first-line\nsecond-line"
        sealed = source.create().seal(value, aad=aad("instance", name))
        with closing(sqlite3.connect(db)) as connection:
            connection.execute(
                "INSERT INTO teatree_sealed_secret (name, key_id, nonce, ciphertext) VALUES (?, ?, ?, ?)",
                (name, sealed.key_id, sealed.nonce, sealed.ciphertext),
            )
            connection.commit()
    if case == "tampered":
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("UPDATE teatree_sealed_secret SET ciphertext = ?", (b"tampered",))
            connection.commit()
    if case == "wrong-key":
        replacement = KeyFileSource(tmp_path / "other.key")
        replacement.create()
        source.path.write_bytes(replacement.path.read_bytes())
    if case == "missing-key":
        source.path.unlink()
    if case == "missing-db":
        db.unlink()
    python_result = _run(db, source, "get", name)
    shell_result = subprocess.run(
        [str(READER), "get", name],
        env={
            **os.environ,
            "T3_SECRET_STORE": "sealed",
            "T3_SECRET_PYTHON": sys.executable,
            "T3_CONFIG_DB": str(db),
            "T3_MASTER_KEY_SOURCE": source.label,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    facade_result = _facade_run(db, source, name)
    for result in (python_result, shell_result, facade_result):
        assert (result.returncode, result.stdout) == (expected_code, expected_output), result.stderr


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("T3_SECRET_STORE=\n", "pass"),
        ("T3_SECRET_STORE=sealed\r\n", "sealed"),
        ("export T3_SECRET_STORE=sealed\n", "sealed"),
        ("T3_SECRET_STORE=sealed\nT3_SECRET_STORE=pass\n", "pass"),
    ],
)
def test_selector_env_file_follows_last_assignment(tmp_path: Path, content: str, expected: str) -> None:
    env_file = tmp_path / "teatree.env"
    env_file.write_text(content)
    result = subprocess.run(
        [str(READER), "selector"],
        env={**os.environ, "T3_SECRET_STORE": "", "TEATREE_SECRET_ENV_FILE": str(env_file)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout) == (0, f"{expected}\n")


def test_deploy_and_roll_host_reads_never_invoke_pass_in_sealed_mode(tmp_path: Path) -> None:
    root = READER.parent
    binary = tmp_path / "bin"
    binary.mkdir()
    stub = binary / "pass"
    stub.write_text('#!/bin/sh\necho "$*" >> "$PASS_LOG"\n')
    stub.chmod(0o755)
    log = tmp_path / "pass.log"
    for filename, start, end in (
        ("deploy.sh", "# The GitLab token compose", "# Services the staged swap"),
        ("roll.sh", 'for pair in "GITLAB_TOKEN:TEATREE_GITLAB_TOKEN_PASS_PATH"', "# The worker's caps"),
    ):
        code = (root / filename).read_text()
        shell = code[code.index(start) : code.index(end, code.index(start))]
        result = subprocess.run(
            [shutil.which("bash") or "/bin/bash", "-c", f"set -euo pipefail\nSCRIPT_DIR={str(root)!r}\n{shell}"],
            env={
                **os.environ,
                "PATH": f"{binary}:{os.environ['PATH']}",
                "PASS_LOG": str(log),
                "T3_SECRET_STORE": "sealed",
                "TEATREE_GITLAB_TOKEN_PASS_PATH": "team/token",
                "NOTION_TOKEN_PASS_PATH": "team/notion",
                "GITLAB_TOKEN": "",
                "NOTION_TOKEN": "",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    assert not log.exists()


def test_deploy_shell_readers_use_the_one_selector_helper() -> None:
    root = READER.parent
    for name in ("t3", "deploy.sh", "roll.sh", "entrypoint.sh"):
        source = (root / name).read_text()
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        assert "secret-read.sh" in code
        assert "pass show" not in code
