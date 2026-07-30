# test-path: cross-cutting — drives deploy/entrypoint.sh (no src mirror).
"""Integration tests for the deploy entrypoint's boot-time pass secret sourcing (#3454).

`deploy/entrypoint.sh` keeps the box's runtime secrets in its gpg-encrypted `pass`
store rather than as plaintext in the compose `env_file`. At boot it sources
`TEATREE_GH_TOKEN` / `T3_ADMIN_PASSWORD` from `pass` via `source_secret_from_pass`
when the corresponding env var is unset, so a rotated secret is picked up without
rewriting `teatree.env`. An existing env value always wins.

These run the REAL shell function (extracted verbatim from the entrypoint) in a
bash subprocess with a stub `pass` on PATH, so the sourcing contract — env wins,
pass is the fallback, a missing entry is a no-op — is exercised end to end.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="needs bash (present in the deploy image and CI)",
)

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"
_BASH = shutil.which("bash") or "bash"


def _extract_shell_function(name: str) -> str:
    """Return the verbatim source of shell function *name* from the entrypoint."""
    body: list[str] = []
    capturing = False
    for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}() {{"):
            capturing = True
        if capturing:
            body.append(line)
            if line == "}":
                return "\n".join(body)
    not_found = f"function {name!r} not found in {ENTRYPOINT}"
    raise AssertionError(not_found)


def _write_pass_stub(bin_dir: Path, entries: dict[str, str]) -> None:
    """A `pass` shim resolving *entries* (path → value); a missing path exits 1."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "pass"
    cases = "".join(f'    {path!r}) printf "%s\\n" {value!r} ;;\n' for path, value in entries.items())
    shim.write_text(
        f'#!/usr/bin/env bash\nif [ "$1" != "show" ]; then exit 2; fi\ncase "$2" in\n{cases}    *) exit 1 ;;\nesac\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _source_secret(tmp_path: Path, entries: dict[str, str], *, var: str, path: str, **env: str) -> tuple[str, int]:
    """Run `source_secret_from_pass <var> <path>` and return (resolved value, rc)."""
    bin_dir = tmp_path / "bin"
    _write_pass_stub(bin_dir, entries)
    func = _extract_shell_function("source_secret_from_pass")
    harness = tmp_path / "harness.sh"
    harness.write_text(
        f'set -euo pipefail\n{func}\nsource_secret_from_pass {var} {path!r}\nprintf "%s" "${{{var}:-}}"\n',
        encoding="utf-8",
    )
    run_env = dict(os.environ)
    run_env["PATH"] = f"{bin_dir}{os.pathsep}{run_env['PATH']}"
    run_env.pop(var, None)
    run_env.update(env)
    proc = subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=False, env=run_env)
    return proc.stdout, proc.returncode


class TestSourceSecretFromPass:
    def test_rotated_pass_token_is_sourced_when_env_unset(self, tmp_path: Path) -> None:
        value, rc = _source_secret(
            tmp_path,
            {"github/souliane/pat": "rotated-token-xyz"},
            var="TEATREE_GH_TOKEN",
            path="github/souliane/pat",
        )
        assert rc == 0
        assert value == "rotated-token-xyz"

    def test_existing_env_value_wins_over_pass(self, tmp_path: Path) -> None:
        value, rc = _source_secret(
            tmp_path,
            {"github/souliane/pat": "token-from-pass"},
            var="TEATREE_GH_TOKEN",
            path="github/souliane/pat",
            TEATREE_GH_TOKEN="token-from-env",
        )
        assert rc == 0
        assert value == "token-from-env"

    def test_missing_pass_entry_is_a_noop(self, tmp_path: Path) -> None:
        # No entry and no env value: the function must leave the var unset and
        # never fail (a box without this secret provisioned must still boot).
        value, rc = _source_secret(
            tmp_path,
            {},
            var="T3_ADMIN_PASSWORD",
            path="teatree/admin-password",
        )
        assert rc == 0
        assert value == ""

    def test_admin_password_sourced_from_pass(self, tmp_path: Path) -> None:
        value, rc = _source_secret(
            tmp_path,
            {"teatree/admin-password": "s3cr3t-admin"},
            var="T3_ADMIN_PASSWORD",
            path="teatree/admin-password",
        )
        assert rc == 0
        assert value == "s3cr3t-admin"
