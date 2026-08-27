"""The containerized `t3` must carry the HOST's GitLab login across the boundary.

`ReviewService.get_gitlab_token()` resolves ``$GITLAB_TOKEN``, then the overlay
config, then ``glab auth status`` — and inside the container all three are empty:
the overlay's token lives in the HOST's ``pass`` store (unreadable there, the gpg
agent and its keys stay on the host) and the container's ``glab`` has never been
logged in. Every GitLab review write then dies on "No GitLab token found. Run:
glab auth login" while the operator's host ``glab`` is authenticated the whole
time. ``deploy/t3`` closes that gap: with ``GITLAB_TOKEN`` unset it resolves the
host login and forwards it as a BARE ``--env GITLAB_TOKEN``, which docker reads
from the wrapper's own environment. The bare form is not a style choice: an argv
is world-readable in the host process table, so ``--env GITLAB_TOKEN=<value>``
published the operator's credential to every local process for the life of the
call. The value must reach the container and must not reach the argv, and only
asserting both together rules out a wrapper that leaks nothing by forwarding
nothing.

The wrapper is exercised for real — the genuine ``deploy/t3`` copied into a
tmp tree, run by ``/usr/bin/env bash`` (the macOS default `bash` 3.2 is the
compatibility floor this script targets), against a ``docker`` stub that reports
the argv it was handed and a ``glab`` stub that models an authenticated host, a
logged-out host, and (by omission from a hermetic PATH) no `glab` at all. Docker
and glab are the unstoppable externals here; nothing else is stubbed.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from _deploy_forwarded_env import ENV_REPORT, argv, forwarded

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"

# Only the system dirs plus the stub dir: the wrapper needs `install`, `awk`,
# `basename`, `dirname` and `bash`, and a REAL `glab` leaking in from the
# operator's PATH would make the absent-glab case untestable.
SYSTEM_PATH = os.defpath.strip(os.pathsep)
FAKE_TOKEN = "stub-host-login-000000000000"

pytestmark = pytest.mark.skipif(
    shutil.which("bash", path=SYSTEM_PATH) is None or shutil.which("awk", path=SYSTEM_PATH) is None,
    reason="needs a system bash + awk (present on macOS, in the deploy image, and in CI)",
)

# `compose ps` answers "nothing running" so the wrapper takes its one-off `run`
# branch; that invocation reports the argv it was handed, one entry per line.
DOCKER_STUB = (
    """#!/usr/bin/env bash
for arg in "$@"; do
    [ "$arg" = ps ] && exit 0
done
printf 'ARG %s\\n' "$@"
"""
    + ENV_REPORT
)

# Models `glab auth status --show-token`. GLAB_STUB_STREAM picks the stream the report
# lands on (glab writes it to stderr on some versions, stdout on others);
# GLAB_STUB_LOGGED_OUT models the unauthenticated host, which exits non-zero with no
# token line. Every call records its argv so a test can prove the wrapper did NOT
# consult glab when the operator exported a token.
GLAB_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$GLAB_STUB_CALLS"
if [ -n "${GLAB_STUB_LOGGED_OUT:-}" ]; then
    echo "X gitlab.com has not been authenticated with glab. Run \\`glab auth login\\`." >&2
    exit 1
fi
REPORT="  ✓ Token found: ${GLAB_STUB_TOKEN}"
if [ "${GLAB_STUB_STREAM:-stderr}" = stdout ]; then
    echo "$REPORT"
else
    echo "$REPORT" >&2
fi
exit 0
"""


def _write_stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _install_wrapper(tmp_path: Path) -> Path:
    entry = tmp_path / "teatree-deploy" / "deploy" / "t3"
    entry.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    return entry


def _run(
    tmp_path: Path,
    *,
    with_glab: bool,
    env_overrides: dict[str, str] | None = None,
    xtrace: bool = False,
) -> subprocess.CompletedProcess[str]:
    stub_dir = tmp_path / "stub-bin"
    _write_stub(stub_dir / "docker", DOCKER_STUB)
    if with_glab:
        _write_stub(stub_dir / "glab", GLAB_STUB)

    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "GITHUB_", "T3_", "TEATREE_"))}
    env["PATH"] = f"{stub_dir}{os.pathsep}{SYSTEM_PATH}"
    env["TEATREE_HOST_HOME"] = str(tmp_path / "home")
    env["GLAB_STUB_CALLS"] = str(tmp_path / "glab-calls.log")
    env["GLAB_STUB_TOKEN"] = FAKE_TOKEN
    env.update(env_overrides or {})

    entry = _install_wrapper(tmp_path)
    bash = shutil.which("bash", path=SYSTEM_PATH) or "bash"
    argv = [bash, "-x", str(entry), "--help"] if xtrace else [str(entry), "--help"]

    # Stand OUTSIDE any checkout: the subject is credential forwarding, and
    # inheriting pytest's cwd would instead trip the invisible-checkout refusal
    # (this repo is a checkout, and `TEATREE_HOST_HOME` is redirected above so it
    # sits under none of the mounts the wrapper computes).
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)

    return subprocess.run(argv, capture_output=True, text=True, check=True, env=env, cwd=elsewhere)


def _invoke(tmp_path: Path, *, with_glab: bool, env_overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Run the real wrapper; return the ``NAME=VALUE`` pairs it forwarded via ``--env``."""
    proc = _run(tmp_path, with_glab=with_glab, env_overrides=env_overrides)
    return forwarded(proc)


def _glab_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "glab-calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


class TestHostGlabLoginReachesTheContainer:
    def test_authenticated_glab_token_is_forwarded(self, tmp_path: Path) -> None:
        assert _invoke(tmp_path, with_glab=True)["GITLAB_TOKEN"] == FAKE_TOKEN

    def test_report_on_stdout_is_read_too(self, tmp_path: Path) -> None:
        # `glab auth status -t` prints its report to stderr on some versions and
        # stdout on others; a stream-specific read works on one machine only.
        forwarded = _invoke(tmp_path, with_glab=True, env_overrides={"GLAB_STUB_STREAM": "stdout"})
        assert forwarded["GITLAB_TOKEN"] == FAKE_TOKEN

    def test_the_host_login_is_actually_consulted(self, tmp_path: Path) -> None:
        _invoke(tmp_path, with_glab=True)
        assert _glab_calls(tmp_path) == ["auth status --show-token"]


class TestNoHostLoginChangesNothing:
    def test_absent_glab_forwards_no_token(self, tmp_path: Path) -> None:
        # The hermetic PATH carries no `glab` at all — the wrapper must fall
        # through silently rather than treat an unresolvable credential as fatal.
        assert "GITLAB_TOKEN" not in _invoke(tmp_path, with_glab=False)

    def test_logged_out_glab_forwards_no_token(self, tmp_path: Path) -> None:
        # glab exits non-zero with an error and no token line; an empty resolution
        # must forward NOTHING rather than an empty GITLAB_TOKEN, which would
        # shadow the service's own environment inside the container.
        forwarded = _invoke(tmp_path, with_glab=True, env_overrides={"GLAB_STUB_LOGGED_OUT": "1"})
        assert "GITLAB_TOKEN" not in forwarded

    def test_absent_glab_still_forwards_the_other_names(self, tmp_path: Path) -> None:
        forwarded = _invoke(tmp_path, with_glab=False, env_overrides={"T3_OVERLAY_NAME": "demo-overlay"})
        assert forwarded["T3_OVERLAY_NAME"] == "demo-overlay"
        # Exact on the NAME set, not the values: TEATREE_DEPLOY_CHECKOUT is the
        # wrapper's own export, and an unresolvable glab must add nothing beyond it.
        assert set(forwarded) == {"T3_OVERLAY_NAME", "TEATREE_DEPLOY_CHECKOUT"}


class TestTheCredentialNeverReachesATrace:
    """`bash -x t3` must not print the token it now resolves without being asked to."""

    def test_xtrace_run_forwards_the_token_without_tracing_it(self, tmp_path: Path) -> None:
        # The `--env NAME=value` pair rides in the argv of the final `docker compose`
        # hop, so a trace restored anywhere before the dispatch prints the credential.
        proc = _run(tmp_path, with_glab=True, xtrace=True)
        assert forwarded(proc)["GITLAB_TOKEN"] == FAKE_TOKEN
        assert FAKE_TOKEN not in proc.stderr

    def test_the_mount_wiring_above_the_credential_region_stays_traceable(self, tmp_path: Path) -> None:
        # Suppression is scoped, not a blanket kill: the layout/mount decisions —
        # the part an operator runs `bash -x` for — are still traced.
        proc = _run(tmp_path, with_glab=True, xtrace=True)
        assert "TEATREE_HOST_HOME=" in proc.stderr


class TestTheCredentialNeverEntersTheHostProcessTable:
    """A `docker` argv is world-readable; a credential in one is published locally."""

    @staticmethod
    def _assert_delivered_but_not_in_argv(proc: subprocess.CompletedProcess[str], token: str) -> None:
        # Both halves, together: "absent from the argv" alone is also what a wrapper
        # that forwards nothing at all would report.
        assert forwarded(proc)["GITLAB_TOKEN"] == token, "the container did not receive the token"
        leaked = [arg for arg in argv(proc) if token in arg]
        assert leaked == [], f"the token value rides in the docker argv: {leaked}"

    def test_a_glab_resolved_token_reaches_the_container_without_entering_the_argv(self, tmp_path: Path) -> None:
        self._assert_delivered_but_not_in_argv(_run(tmp_path, with_glab=True), FAKE_TOKEN)

    def test_an_exported_token_reaches_the_container_without_entering_the_argv(self, tmp_path: Path) -> None:
        # The operator-exported path assembles the same flags, so it leaks identically.
        exported = "stub-operators-own-choice"
        proc = _run(tmp_path, with_glab=True, env_overrides={"GITLAB_TOKEN": exported})
        self._assert_delivered_but_not_in_argv(proc, exported)


class TestExportedTokenWins:
    def test_exported_token_is_forwarded_untouched(self, tmp_path: Path) -> None:
        exported = "stub-operators-own-choice"
        forwarded = _invoke(tmp_path, with_glab=True, env_overrides={"GITLAB_TOKEN": exported})
        assert forwarded["GITLAB_TOKEN"] == exported

    def test_exported_token_skips_the_glab_call(self, tmp_path: Path) -> None:
        # An operator's stated choice is authoritative, so there is nothing to
        # resolve — and resolving anyway would spend a network round trip on
        # every single `t3` invocation.
        _invoke(tmp_path, with_glab=True, env_overrides={"GITLAB_TOKEN": "stub-operators-own-choice"})
        assert _glab_calls(tmp_path) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
