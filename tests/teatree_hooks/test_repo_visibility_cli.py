"""The shell seam resolves visibility on the forge the remote actually lives on.

The pre-push leak gate (:file:`scripts/hooks/refuse-public-push-with-leak.sh`)
used to shell ``gh repo view`` for EVERY remote. That hard-codes one forge: a
``gitlab.com`` remote made ``gh`` error out on the namespace, so visibility came
back undetermined on every single push and the gate fell into its fail-closed
branch -- scanning a PRIVATE repo, forever, on every push.

These tests drive the real CLI as a subprocess with real ``gh``/``glab`` shims on
``PATH`` (nothing about the routing is mocked) and pin:

- a GitLab remote routes to ``glab`` and resolves -- the regression that made the
    gate unable to classify its own remote,
- a GitHub remote still routes to ``gh`` -- the no-regression control,
- an unresolvable remote yields ``UNKNOWN``, the fail-safe the gate must read as
    "keep scanning" rather than "skip",
- the verdict is cached per slug, so a repeat push pays no probe at all.

The ``UNKNOWN`` row is the anti-vacuity guard: it proves a resolved verdict comes
from the probe actually answering, not from the CLI defaulting to something
permissive.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_GITLAB_REMOTE = "git@gitlab.com:acme-eng/inner/widget.git"
_GITHUB_REMOTE = "https://github.com/acme/widget.git"


def _write_shim(bin_dir: Path, name: str, body: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / name
    shim.write_text(body, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _forge_shims(bin_dir: Path, *, gh_visibility: str = "PUBLIC", glab_visibility: str = "private") -> Path:
    """Real ``gh``/``glab`` shims that also RECORD which one was invoked."""
    log = bin_dir / "invocations.log"
    _write_shim(
        bin_dir,
        "gh",
        "#!/usr/bin/env bash\n"
        f'echo "gh $*" >> "{log}"\n'
        'if [[ "$*" == *"repo view"* && "$*" == *"visibility"* ]]; then\n'
        f'  echo "{gh_visibility}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )
    _write_shim(
        bin_dir,
        "glab",
        "#!/usr/bin/env bash\n"
        f'echo "glab $*" >> "{log}"\n'
        'if [[ "$*" == *"api"* && "$*" == *"projects/"* ]]; then\n'
        f'  echo \'{{"visibility":"{glab_visibility}"}}\'\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )
    return log


def _run_cli(remote: str, tmp_path: Path, bin_dir: Path) -> str:
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        # Isolate the day cache so a verdict never leaks between tests or in
        # from the developer's own machine.
        "T3_DATA_DIR": str(tmp_path / "state"),
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "teatree.hooks.repo_visibility_cli", remote],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


class TestVisibilityRoutesToTheRemotesOwnForge:
    def test_gitlab_remote_resolves_via_glab(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        log = _forge_shims(bin_dir, glab_visibility="private")

        assert _run_cli(_GITLAB_REMOTE, tmp_path, bin_dir) == "PRIVATE"
        assert "glab" in log.read_text(encoding="utf-8")

    def test_gitlab_remote_never_asks_github(self, tmp_path: Path) -> None:
        """The exact defect: a GitLab remote sent to ``gh`` can never resolve."""
        bin_dir = tmp_path / "bin"
        log = _forge_shims(bin_dir, glab_visibility="private")

        _run_cli(_GITLAB_REMOTE, tmp_path, bin_dir)

        assert not any(line.startswith("gh ") for line in log.read_text(encoding="utf-8").splitlines())

    def test_github_remote_still_resolves_via_gh(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        log = _forge_shims(bin_dir, gh_visibility="PUBLIC")

        assert _run_cli(_GITHUB_REMOTE, tmp_path, bin_dir) == "PUBLIC"
        assert any(line.startswith("gh ") for line in log.read_text(encoding="utf-8").splitlines())

    @pytest.mark.parametrize(
        "remote",
        [
            "not-a-remote",
            "",
            "https://example.invalid/",
        ],
    )
    def test_unresolvable_remote_is_unknown(self, remote: str, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _forge_shims(bin_dir)

        assert _run_cli(remote, tmp_path, bin_dir) == "UNKNOWN"

    def test_absent_forge_cli_is_unknown_not_public(self, tmp_path: Path) -> None:
        """No forge tool must yield the fail-safe UNKNOWN, never a permissive verdict."""
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()

        assert _run_cli(_GITLAB_REMOTE, tmp_path, empty_bin) == "UNKNOWN"


class TestVisibilityVerdictIsCachedPerRemote:
    def test_second_resolution_makes_no_probe(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        log = _forge_shims(bin_dir, glab_visibility="private")

        assert _run_cli(_GITLAB_REMOTE, tmp_path, bin_dir) == "PRIVATE"
        probes_after_first = len(log.read_text(encoding="utf-8").splitlines())
        assert _run_cli(_GITLAB_REMOTE, tmp_path, bin_dir) == "PRIVATE"

        assert len(log.read_text(encoding="utf-8").splitlines()) == probes_after_first

    def test_cache_is_keyed_by_host_qualified_slug(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _forge_shims(bin_dir, glab_visibility="private")
        _run_cli(_GITLAB_REMOTE, tmp_path, bin_dir)

        cache = json.loads((tmp_path / "state" / "repo-visibility-cache.json").read_text(encoding="utf-8"))

        assert "gitlab.com/acme-eng/inner/widget" in cache
