# test-path: cross-cutting — runs the git-core key pin lines of deploy/Dockerfile; no src mirror.
"""Only the pinned git-core PPA key may reach apt, and any other shape fails the image build.

The Dockerfile imports whatever the keyserver answers into a throwaway keyring, exports ONLY the
pinned fingerprint into apt's keyring, and refuses unless that keyring holds one primary key whose
OWN fingerprint is the pin. These run the Dockerfile's own lines against real gpg keys.
"""

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[1] / "deploy" / "Dockerfile"
APT_KEYRING = "/etc/apt/keyrings/git-core.gpg"
STEP_START = 'gpg --homedir "$git_core_gnupg" --batch --quiet --import'
STEP_END = "gpgconf --homedir"


def _tool(name: str) -> str:
    found = shutil.which(name)
    assert found, f"the pin step needs {name}, which the CI image and the deploy image both ship"
    return found


def _gpg(home: Path, *args: str, stdin: str = "", check: bool = True) -> str:
    return subprocess.run(
        [_tool("gpg"), "--homedir", str(home), "--batch", "--quiet", "--pinentry-mode", "loopback", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=check,
        timeout=120,
    ).stdout


def _fingerprints_after(record: str, colon_listing: str) -> list[str]:
    found: list[str] = []
    armed = False
    for line in colon_listing.splitlines():
        fields = line.split(":")
        if fields[0] == record:
            armed = True
        elif armed and fields[0] == "fpr":
            found.append(fields[9])
            armed = False
    return found


def _fingerprints(home: Path, target: str, *, record: str) -> list[str]:
    listing = _gpg(home, "--with-colons", "--with-subkey-fingerprint", "--list-keys", target)
    return _fingerprints_after(record, listing)


def _pin_step(keyring: Path) -> str:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    start = dockerfile.index(STEP_START)
    lines = [
        ln
        for ln in dockerfile[start : dockerfile.index(STEP_END, start)].splitlines()
        if not ln.lstrip().startswith("#")
    ]
    return re.sub(r"\s*&&\s*\\$", "", "\n".join(lines).rstrip()).replace(APT_KEYRING, str(keyring))


@dataclass(frozen=True)
class _Keyserver:
    pinned: str
    extra_subkey: str
    answers: dict[str, str]


@pytest.fixture(scope="module")
def keyserver() -> Iterator[_Keyserver]:
    home = Path(tempfile.mkdtemp(prefix="gpg-"))
    try:
        for uid in ("pinned-key", "extra-key"):
            _gpg(home, "--passphrase", "", "--quick-generate-key", uid, "ed25519", "default", "0")
        (pinned,) = _fingerprints(home, "pinned-key", record="pub")
        (extra,) = _fingerprints(home, "extra-key", record="pub")
        _gpg(home, "--passphrase", "", "--quick-add-key", extra, "cv25519", "encr", "0")
        (extra_subkey,) = _fingerprints(home, "extra-key", record="sub")
        _gpg(
            home,
            "--gen-key",
            stdin=f"%no-protection\nKey-Type: EDDSA\nKey-Curve: ed25519\nName-Real: revoker-holder\n"
            f"Revoker: 1:{pinned}\nExpire-Date: 0\n%commit\n",
        )
        (revoker_holder,) = _fingerprints(home, "revoker-holder", record="pub")
        armored = {
            name: _gpg(home, "--armor", "--export", fpr)
            for name, fpr in (
                ("pinned", pinned),
                ("extra", extra),
                ("revoker_holder", revoker_holder),
            )
        }
        yield _Keyserver(
            pinned=pinned,
            extra_subkey=extra_subkey,
            answers={
                **armored,
                "pinned_and_extra": armored["pinned"] + armored["extra"],
                "garbage": "not a key\n",
                "empty": "",
            },
        )
    finally:
        subprocess.run([_tool("gpgconf"), "--homedir", str(home), "--kill", "all"], check=False, timeout=60)
        shutil.rmtree(home, ignore_errors=True)


@dataclass(frozen=True)
class _StepRun:
    failed: bool
    exported_primary_keys: list[str]


def _run_pin_step(*, answer: str, pin: str, tmp_path: Path) -> _StepRun:
    scratch = Path(tempfile.mkdtemp(prefix="gpg-"))
    keyring = tmp_path / "git-core.gpg"
    try:
        (scratch / "fetched.asc").write_text(answer, encoding="utf-8")
        script = f"git_core_ppa_fingerprint={pin}\ngit_core_gnupg={scratch}\n{_pin_step(keyring)}\n"
        unusable_ambient_keyring = str(tmp_path / "missing" / "gnupg-home")
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GNUPGHOME": unusable_ambient_keyring}
        step = subprocess.run(
            [_tool("bash"), "-eo", "pipefail", "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=120,
        )
        exported = _gpg(scratch, "--with-colons", "--show-keys", str(keyring), check=False)
        return _StepRun(failed=step.returncode != 0, exported_primary_keys=_fingerprints_after("pub", exported))
    finally:
        subprocess.run([_tool("gpgconf"), "--homedir", str(scratch), "--kill", "all"], check=False, timeout=60)
        shutil.rmtree(scratch, ignore_errors=True)


def test_the_pin_is_a_full_fingerprint_not_a_short_id() -> None:
    assert re.search(r"git_core_ppa_fingerprint=[0-9A-F]{40} &&", DOCKERFILE.read_text(encoding="utf-8"))


def test_the_keyserver_fetch_retries_like_the_images_other_fetches() -> None:
    assert re.search(
        r'curl -fsSL --retry \d+ --retry-all-errors "https://keyserver', DOCKERFILE.read_text(encoding="utf-8")
    )


def test_the_pinned_key_passes_and_is_the_only_key_exported(keyserver: _Keyserver, tmp_path: Path) -> None:
    run = _run_pin_step(answer=keyserver.answers["pinned"], pin=keyserver.pinned, tmp_path=tmp_path)

    assert not run.failed
    assert run.exported_primary_keys == [keyserver.pinned]


def test_a_keyserver_answer_carrying_extra_keys_exports_only_the_pinned_one(
    keyserver: _Keyserver, tmp_path: Path
) -> None:
    run = _run_pin_step(answer=keyserver.answers["pinned_and_extra"], pin=keyserver.pinned, tmp_path=tmp_path)

    assert not run.failed
    assert run.exported_primary_keys == [keyserver.pinned]


def test_a_key_naming_the_pin_as_its_revoker_does_not_stand_in_for_it(keyserver: _Keyserver, tmp_path: Path) -> None:
    run = _run_pin_step(answer=keyserver.answers["revoker_holder"], pin=keyserver.pinned, tmp_path=tmp_path)

    assert run.failed


def test_a_pin_that_is_only_a_subkey_fingerprint_fails(keyserver: _Keyserver, tmp_path: Path) -> None:
    run = _run_pin_step(answer=keyserver.answers["extra"], pin=keyserver.extra_subkey, tmp_path=tmp_path)

    assert run.failed


@pytest.mark.parametrize("answer", ["garbage", "empty"])
def test_an_answer_that_is_not_a_key_fails(keyserver: _Keyserver, answer: str, tmp_path: Path) -> None:
    run = _run_pin_step(answer=keyserver.answers[answer], pin=keyserver.pinned, tmp_path=tmp_path)

    assert run.failed
