"""The in-place pass before every eval upload leaves only files it proved clean on disk.

It is the defense-in-depth step over exactly the files an upload publishes: each
file is rewritten redacted and re-read, and one it cannot prove clean is deleted,
never left as it was, and fails the step every upload is gated on.
"""

import errno
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from teatree.eval.artifact_redaction import REDACTED, redact_in_place_main
from tests.teatree_eval._redaction_fakes import API_KEY, POOL, SUBSCRIPTION, set_fake_credentials


@pytest.fixture(autouse=True)
def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    set_fake_credentials(monkeypatch)


def test_the_in_place_pass_redacts_every_file_an_upload_publishes(tmp_path: Path) -> None:
    uploads = tmp_path / "runner-temp"
    uploads.mkdir()
    (uploads / "eval-transcripts-alpha.html").write_text(f"<pre>{SUBSCRIPTION}</pre>", encoding="utf-8")
    (uploads / "eval-transcripts-beta.html").write_text(f"hook output: {POOL[0][:20]}", encoding="utf-8")
    (uploads / "eval-run-leg.log").write_text(f"error: {API_KEY}\n", encoding="utf-8")
    (uploads / "not-uploaded.txt").write_text(API_KEY, encoding="utf-8")
    patterns = ["eval-transcripts-*.html", "eval-run-leg.log", "eval-summary-never-written.md"]

    assert redact_in_place_main([str(uploads / pattern) for pattern in patterns]) == 0

    assert {path.name: path.read_text(encoding="utf-8") for path in uploads.iterdir()} == {
        "eval-transcripts-alpha.html": f"<pre>{REDACTED}</pre>",
        "eval-transcripts-beta.html": f"hook output: {REDACTED}",
        "eval-run-leg.log": f"error: {REDACTED}\n",
        "not-uploaded.txt": API_KEY,
    }


def _failing_open(target: Path, failing_mode: str) -> Any:
    """`Path.open` that raises an I/O error for *target* opened in *failing_mode*, and opens everything else."""
    real_open = Path.open

    def open_(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path == target and mode.startswith(failing_mode):
            raise OSError(errno.EIO, "Input/output error", str(path))
        return real_open(path, mode, *args, **kwargs)

    return patch.object(Path, "open", open_)


def _files_holding(root: Path, value: str) -> list[str]:
    return [str(path) for path in root.rglob("*") if path.is_file() and value in path.read_text(encoding="utf-8")]


@pytest.mark.parametrize("mode", ["r", "w"], ids=["read-error", "write-error"])
def test_a_file_the_pass_cannot_rewrite_is_deleted_and_the_pass_fails(
    mode: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken, other = tmp_path / "eval-transcripts-alpha.html", tmp_path / "eval-transcripts-beta.html"
    broken.write_text(f"<pre>{SUBSCRIPTION}</pre>", encoding="utf-8")
    other.write_text(f"error: {API_KEY}\n", encoding="utf-8")

    with _failing_open(broken, failing_mode=mode):
        rc = redact_in_place_main([str(tmp_path / "eval-transcripts-*.html")])

    assert rc != 0
    assert not broken.exists(), "an unredacted file was left on disk for the upload step"
    assert other.read_text(encoding="utf-8") == f"error: {REDACTED}\n"
    assert _files_holding(tmp_path, SUBSCRIPTION) == []
    assert str(broken) in capsys.readouterr().err


def test_an_error_that_stops_the_pass_deletes_every_file_it_has_not_proven_clean(tmp_path: Path) -> None:
    # Not an I/O error the pass counts and moves past: it propagates, and every file
    # the pass had not yet proven clean must be gone from disk before it does.
    first, later = tmp_path / "eval-transcripts-alpha.html", tmp_path / "eval-transcripts-beta.html"
    first.write_text(f"<pre>{SUBSCRIPTION}</pre>", encoding="utf-8")
    later.write_text(f"error: {API_KEY}\n", encoding="utf-8")
    real_read_text = Path.read_text

    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == first:
            raise MemoryError
        return real_read_text(path, *args, **kwargs)

    with patch.object(Path, "read_text", read_text), pytest.raises(MemoryError):
        redact_in_place_main([str(tmp_path / "eval-transcripts-*.html")])

    assert (first.exists(), later.exists()) == (False, False), "a file the pass never proved clean was left on disk"


def test_a_file_the_pass_cannot_delete_fails_it_by_name_and_the_others_are_still_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stuck, other = tmp_path / "eval-transcripts-alpha.html", tmp_path / "eval-transcripts-beta.html"
    stuck.write_text(f"<pre>{SUBSCRIPTION}</pre>", encoding="utf-8")
    other.write_text(f"error: {API_KEY}\n", encoding="utf-8")
    real_unlink = Path.unlink

    def unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == stuck:
            raise OSError(errno.EACCES, "Permission denied", str(path))
        real_unlink(path, missing_ok=missing_ok)

    with _failing_open(stuck, failing_mode="r"), patch.object(Path, "unlink", unlink):
        rc = redact_in_place_main([str(tmp_path / "eval-transcripts-*.html")])

    assert rc != 0
    assert other.read_text(encoding="utf-8") == f"error: {REDACTED}\n"
    err = capsys.readouterr().err
    assert err.count("\n") == err.count(str(stuck)) == 1
    assert SUBSCRIPTION not in err


def test_a_file_still_holding_a_credential_after_its_rewrite_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A named credential is redacted from 8 characters, so one the marker itself
    # spells survives every rewrite: the re-read still holds it, and the file must not ship.
    spelled_by_marker = REDACTED.strip("[]")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", spelled_by_marker)
    unprovable, clean = tmp_path / "eval-run-leg.log", tmp_path / "eval-summary-leg.md"
    unprovable.write_text(f"Authorization: Bearer {spelled_by_marker}\n", encoding="utf-8")
    clean.write_text("| scenario | verdict |\n", encoding="utf-8")

    rc = redact_in_place_main([str(unprovable), str(clean)])

    assert rc != 0
    assert not unprovable.exists(), "a file the pass could not prove clean was left on disk for the upload step"
    assert clean.read_text(encoding="utf-8") == "| scenario | verdict |\n"


@pytest.mark.parametrize("target", ["a-file", "dangling"])
def test_the_pass_never_follows_a_symlink_and_removes_it(target: str, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "notes.txt"
    outside.parent.mkdir()
    if target == "a-file":
        outside.write_text(f"mine: {API_KEY}\n", encoding="utf-8")
    uploads = tmp_path / "runner-temp"
    uploads.mkdir()
    link = uploads / "eval-run-leg.log"
    link.symlink_to(outside)

    rc = redact_in_place_main([str(link)])

    assert rc != 0
    assert not link.is_symlink(), "a symlink was left in the upload paths"
    assert target == "dangling" or outside.read_text(encoding="utf-8") == f"mine: {API_KEY}\n", "wrote through it"


_MODULE = (sys.executable, "-m", "teatree.eval.artifact_redaction")


def _module(*argv: str, cwd: Path, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([*_MODULE, *argv], input=stdin, capture_output=True, check=False, cwd=cwd)


def test_the_installed_module_redacts_from_a_checkout_without_the_eval_scripts(tmp_path: Path) -> None:
    # A reusable workflow runs in its caller's checkout: an overlay has no scripts/eval/.
    log = tmp_path / "eval-run-pr.log"

    tee = _module("tee", str(log), cwd=tmp_path, stdin=f"error: {API_KEY}\n".encode())
    log.write_text(f"appended raw: {SUBSCRIPTION}\n", encoding="utf-8")
    in_place = _module("in-place", str(log), cwd=tmp_path)

    assert (tee.returncode, tee.stdout.decode()) == (0, f"error: {REDACTED}\n")
    assert in_place.returncode == 0
    assert log.read_text(encoding="utf-8") == f"appended raw: {REDACTED}\n"


@pytest.mark.parametrize("argv", [(), ("teee",)], ids=["no-command", "unknown-command"])
def test_the_module_entry_refuses_an_unknown_command(argv: tuple[str, ...], tmp_path: Path) -> None:
    refused = _module(*argv, cwd=tmp_path)

    assert refused.returncode == 2
    assert b"tee" in refused.stderr
