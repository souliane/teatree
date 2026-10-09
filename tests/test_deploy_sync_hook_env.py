# test-path: cross-cutting — drives deploy/sync-hook-env.sh (no src mirror).
"""The host hook tool env follows the checkout a deploy fast-forwarded, or the deploy fails loud.

The hooks import teatree from the checkout's ``src/`` and every dependency from
``<UV_TOOL_DIR>/teatree``, which nothing else on a docker host re-syncs. Runs the REAL
script against a fake env under ``tmp_path`` whose ``bin/python`` wraps this interpreter,
with a stub uv that can drop the missing dist into the fake env's site dir.
"""

import os
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "deploy" / "sync-hook-env.sh"
_RESOLVE_UV = _ROOT / "scripts" / "hooks" / "lib" / "resolve-uv.sh"
_BASH = shutil.which("bash") or "bash"

_FAKE_DEP = "t5764-fake-dep"
_FAKE_DIST = "t5764_fake_dep"

_SHELL_ESSENTIALS = (
    "bash",
    "sh",
    "env",
    "uname",
    "tr",
    "head",
    "sort",
    "cut",
    "basename",
    "dirname",
    "mkdir",
    "sed",
    "cat",
)

_PYTHON_WRAPPER = f"""#!/bin/sh
printf 'python %s\\n' "$*" >>"$STUB_LOG"
if [ "${{FAKE_PY_CRASH:-}}" = 1 ] && [ "$1" = -m ]; then exit 1; fi
PYTHONPATH="${{PYTHONPATH:+$PYTHONPATH:}}$FAKE_SITE" exec "{sys.executable}" "$@"
"""

_UV_STUB = """#!/usr/bin/env bash
printf 'uv %s\\n' "$*" >>"$STUB_LOG"
printf 'UV_PROJECT_ENVIRONMENT=%s\\n' "${UV_PROJECT_ENVIRONMENT:-}" >>"$STUB_LOG"
if [ -n "${STUB_UV_INSTALLS:-}" ]; then
    dist="$FAKE_SITE/$STUB_UV_INSTALLS-1.0.dist-info"
    mkdir -p "$dist"
    printf 'Metadata-Version: 2.1\\nName: %s\\nVersion: 1.0\\n' "$STUB_UV_INSTALLS" >"$dist/METADATA"
fi
exit "${STUB_UV_EXIT:-0}"
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@dataclass
class _Host:
    checkout: Path
    hook_env: Path
    log: Path
    env: dict[str, str]

    def own(self, editable: Path) -> None:
        (self.hook_env / "uv-receipt.toml").write_text(
            f'[tool]\nrequirements = [{{ name = "teatree", editable = "{editable}" }}]\n', encoding="utf-8"
        )

    def run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [_BASH, str(_SCRIPT), str(self.checkout)],
            capture_output=True,
            text=True,
            env={**self.env, **overrides},
            check=False,
            timeout=120,
        )

    def calls(self, prefix: str) -> list[str]:
        lines = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return [line for line in lines if line.startswith(prefix)]

    @property
    def fix_argv(self) -> list[str]:
        return [
            f"UV_PROJECT_ENVIRONMENT={self.hook_env}",
            *("uv", "sync", "--project", str(self.checkout.resolve())),
            *("--frozen", "--no-default-groups", "--inexact", "--python", f"{self.hook_env}/bin/python"),
        ]


def _build_host(root: Path) -> _Host:
    checkout = root / "checkout"
    (checkout / "scripts" / "hooks" / "lib").mkdir(parents=True)
    shutil.copy2(_RESOLVE_UV, checkout / "scripts" / "hooks" / "lib" / "resolve-uv.sh")
    (checkout / "src").symlink_to(_ROOT / "src")
    (checkout / "pyproject.toml").write_text(
        f'[project]\nname = "teatree"\ndependencies = ["{_FAKE_DEP}>=1"]\n', encoding="utf-8"
    )

    tools = root / "tools"
    hook_env = tools / "teatree"
    (hook_env / "bin").mkdir(parents=True)
    _write_exec(hook_env / "bin" / "python", _PYTHON_WRAPPER)
    stub_bin = root / "stub-bin"
    stub_bin.mkdir()
    _write_exec(stub_bin / "uv", _UV_STUB)
    (root / "hook-site").mkdir()
    (root / "home").mkdir(exist_ok=True)

    log = root / "calls.log"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root / "home"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "UV_TOOL_DIR": str(tools),
        "T3_UV": str(stub_bin / "uv"),
        "STUB_LOG": str(log),
        "FAKE_SITE": str(root / "hook-site"),
    }
    owned = _Host(checkout=checkout, hook_env=hook_env, log=log, env=env)
    owned.own(checkout)
    return owned


@pytest.fixture
def host(tmp_path: Path) -> _Host:
    return _build_host(tmp_path)


def _fatal(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in proc.stderr.splitlines() if "deploy: FATAL" in line]


def _printed_fix(fatal_line: str) -> list[str]:
    return shlex.split(fatal_line.rpartition("fix: ")[2])


class TestAnOwnedStaleEnvIsSyncedThenVerified:
    def test_the_sync_is_one_uv_sync_into_the_tool_env_and_the_verify_passes(self, host: _Host) -> None:
        proc = host.run(STUB_UV_INSTALLS=_FAKE_DIST)

        assert proc.returncode == 0, proc.stderr
        assert "re-synced" in proc.stdout
        assert host.calls("uv ") == [
            (
                f"uv sync --project {host.checkout.resolve()} --frozen --no-default-groups --inexact "
                f"--python {host.hook_env}/bin/python"
            )
        ]
        assert host.calls("UV_PROJECT_ENVIRONMENT=") == [f"UV_PROJECT_ENVIRONMENT={host.hook_env}"]

    def test_an_env_still_stale_after_the_sync_fails_with_the_skew_and_the_fix(self, host: _Host) -> None:
        proc = host.run()

        fatal = _fatal(proc)
        assert proc.returncode == 1
        assert len(fatal) == 1, proc.stderr
        assert "still stale" in fatal[0]
        assert f"{_FAKE_DEP} declares '>=1'" in fatal[0]
        assert _printed_fix(fatal[0]) == host.fix_argv

    def test_a_failed_sync_fails_with_the_fix_and_never_verifies(self, host: _Host) -> None:
        proc = host.run(STUB_UV_EXIT="1")

        fatal = _fatal(proc)
        assert proc.returncode == 1
        assert len(fatal) == 1, proc.stderr
        assert _printed_fix(fatal[0]) == host.fix_argv
        assert host.calls("python -m ") == []

    def test_the_printed_fix_survives_a_path_with_a_space(self, tmp_path: Path) -> None:
        spaced = _build_host(tmp_path / "with space")

        proc = spaced.run(STUB_UV_EXIT="1")

        fatal = _fatal(proc)
        assert proc.returncode == 1
        assert len(fatal) == 1, proc.stderr
        assert _printed_fix(fatal[0]) == spaced.fix_argv

    def test_a_crashed_verify_is_reported_as_unverified_never_as_stale(self, host: _Host) -> None:
        proc = host.run(STUB_UV_INSTALLS=_FAKE_DIST, FAKE_PY_CRASH="1")

        fatal = _fatal(proc)
        assert proc.returncode == 1
        assert len(fatal) == 1, proc.stderr
        assert "could not verify" in fatal[0]
        assert "still stale" not in proc.stdout + proc.stderr


def _drop_the_env(host: _Host, _tmp_path: Path) -> None:
    (host.hook_env / "bin" / "python").unlink()


def _own_from_another_checkout(host: _Host, tmp_path: Path) -> None:
    other = tmp_path / "other-checkout"
    other.mkdir()
    host.own(other)


class TestOwnership:
    @pytest.mark.parametrize(
        "arrange",
        [pytest.param(_drop_the_env, id="no-env"), pytest.param(_own_from_another_checkout, id="foreign-owner")],
    )
    def test_an_env_this_checkout_does_not_own_is_left_alone(
        self, arrange: Callable[[_Host, Path], None], host: _Host, tmp_path: Path
    ) -> None:
        arrange(host, tmp_path)

        proc = host.run(STUB_UV_INSTALLS=_FAKE_DIST)

        assert proc.returncode == 0, proc.stderr
        assert len((proc.stdout + proc.stderr).splitlines()) == 1
        assert host.calls("uv ") == []

    def test_a_receipt_spelling_the_checkout_through_a_symlink_is_owned(self, host: _Host, tmp_path: Path) -> None:
        link = tmp_path / "checkout-link"
        link.symlink_to(host.checkout)
        host.own(link)

        proc = host.run(STUB_UV_INSTALLS=_FAKE_DIST)

        assert proc.returncode == 0, proc.stderr
        assert len(host.calls("uv ")) == 1

    def test_a_comment_cannot_claim_ownership_of_a_foreign_env(self, host: _Host) -> None:
        (host.hook_env / "uv-receipt.toml").write_text(
            f'[tool]\nrequirements = [{{ name = "other", editable = "/other" }}]\n'
            f'# name = "teatree", editable = "{host.checkout}"\n',
            encoding="utf-8",
        )

        proc = host.run(STUB_UV_INSTALLS=_FAKE_DIST)

        assert proc.returncode == 0, proc.stderr
        assert host.calls("uv ") == []

    def test_an_invalid_receipt_fails_loud_before_touching_the_env(self, host: _Host) -> None:
        (host.hook_env / "uv-receipt.toml").write_text("[tool\n", encoding="utf-8")

        proc = host.run(STUB_UV_INSTALLS=_FAKE_DIST)

        assert proc.returncode == 1
        assert len(_fatal(proc)) == 1, proc.stderr
        assert "could not read" in proc.stderr
        assert host.calls("uv ") == []


def test_no_resolvable_uv_fails_loud_naming_uv(host: _Host, tmp_path: Path) -> None:
    bare_bin = tmp_path / "bare-bin"
    bare_bin.mkdir()
    for tool in _SHELL_ESSENTIALS:
        if source := shutil.which(tool):
            (bare_bin / tool).symlink_to(source)
    del host.env["T3_UV"]

    proc = host.run(PATH=str(bare_bin))

    fatal = _fatal(proc)
    assert proc.returncode == 1
    assert len(fatal) == 1, proc.stderr
    assert "no working uv" in fatal[0]


def test_a_failed_uv_candidate_walk_reports_the_resolver_not_a_missing_install(host: _Host) -> None:
    (host.checkout / "scripts" / "hooks" / "lib" / "resolve-uv.sh").write_text(
        "resolve_uv() { return 2; }\n", encoding="utf-8"
    )

    proc = host.run()

    assert proc.returncode == 1
    assert len(_fatal(proc)) == 1, proc.stderr
    assert "could not resolve uv" in proc.stderr
    assert "install uv" not in proc.stderr
    assert host.calls("uv ") == []
