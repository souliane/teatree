# test-path: cross-cutting
"""Drain-then-deploy guardrails: the deploy plane never kills an in-flight agent.

Pins the two halves of the rolling deploy across the deploy artifacts so a future
edit cannot silently drop them.

Piece A (debounce): ``deploy.yml`` serializes on a fixed ``deploy`` group and NEVER
cancels a running convergence (``cancel-in-progress: false``); ``deploy.sh``
fast-forwards the checkout to latest main.

Piece B (drain): ``deploy.sh`` drains the running worker before the image swap;
``entrypoint.sh`` clears ``worker_quiescing`` on the fresh worker so admission
resumes; the worker gets a stop grace window for a clean shutdown.
"""

import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_DEPLOY_YML = _ROOT / ".github" / "workflows" / "deploy.yml"
_DEPLOY_SH = _ROOT / "deploy" / "deploy.sh"
_FF_CHECKOUT_SH = _ROOT / "deploy" / "fast-forward-checkout.sh"
_ENTRYPOINT_SH = _ROOT / "deploy" / "entrypoint.sh"
_COMPOSE_YML = _ROOT / "deploy" / "docker-compose.yml"
#: The shortest measured drain-to-broken-pipe interval across the three failed deploys
#: (276.8s / 280.0s), i.e. the idle window the transport is known NOT to outlive.
_OBSERVED_IDLE_TEARDOWN_SECONDS = 276

#: Anchors bounding deploy.sh's stranded-gate fail-safe, so the signal probe below runs
#: the SHIPPED code rather than a re-typed copy of it.
_FAIL_SAFE_START = "_DRAINED=false"
_FAIL_SAFE_END = "trap _clear_quiescing_if_stranded EXIT"

#: Anchors bounding deploy.sh's `compose` helper, which the fail-safe calls (#4193 wired
#: the host-identity overlay behind it). Lifted verbatim for the same reason the
#: fail-safe is: a re-typed copy would keep passing after the shipped code changed.
_COMPOSE_HELPER_START = "CONTAINER_HOME="
_COMPOSE_HELPER_END = "compose() {"


def _slice(body: str, start_anchor: str, end_anchor: str, what: str) -> str:
    start, end = body.find(start_anchor), body.find(end_anchor)
    moved = f"deploy.sh's {what} moved — re-anchor this probe"
    assert start != -1, moved
    assert end > start, moved
    return body[start : end + len(end_anchor)]


def _compose_helper_block() -> str:
    """deploy.sh's `compose` wrapper plus the constants it reads, verbatim.

    The fail-safe calls `compose`, not `docker compose`, so the harness has to carry the
    real definition — otherwise the probe would prove a function it invented.
    """
    body = _DEPLOY_SH.read_text(encoding="utf-8")
    head = _slice(body, _COMPOSE_HELPER_START, _COMPOSE_HELPER_END, "compose helper")
    rest = body[body.find(_COMPOSE_HELPER_END) + len(_COMPOSE_HELPER_END) :]
    closing = rest.find("\n}\n")
    assert closing != -1, "deploy.sh's compose() helper is not closed as expected — re-anchor this probe"
    return f"{head}{rest[: closing + len('\n}\n')]}\n"


def _fail_safe_block() -> str:
    """deploy.sh's stranded-gate fail-safe, verbatim, anchors included."""
    return _slice(_DEPLOY_SH.read_text(encoding="utf-8"), _FAIL_SAFE_START, _FAIL_SAFE_END, "stranded-gate fail-safe")


def _run_fail_safe_under_signal(tmp_path: Path, sig: int, *, fail_safe: str) -> list[str]:
    """Signal a script carrying *fail_safe* mid-drain; return the `docker` calls it made."""
    docker_log = tmp_path / "docker.log"
    ready = tmp_path / "ready"
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "docker"
    stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{docker_log}"\n', encoding="utf-8")
    stub.chmod(0o755)

    script = tmp_path / "harness.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nCOMPOSE_FILE=/dev/null\nHOST_IDENTITY_FILE=/dev/null\n"
        f"{_compose_helper_block()}"
        f"{fail_safe}\n"
        f'_DRAINED=true\ntouch "{ready}"\nsleep 5\n',
        encoding="utf-8",
    )

    env = {**os.environ, "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}"}
    proc = subprocess.Popen(["bash", str(script)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S607 — a fixture-authored script under tmp_path
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "the harness never reached its drain"
        proc.send_signal(sig)
        proc.wait(timeout=10)
    finally:
        proc.kill()
    return docker_log.read_text(encoding="utf-8").splitlines() if docker_log.exists() else []


def _deploy_workflow() -> dict:
    return yaml.safe_load(_DEPLOY_YML.read_text(encoding="utf-8"))


class TestDeployDebounce:
    def test_concurrency_group_is_the_fixed_deploy_group(self) -> None:
        assert str(_deploy_workflow()["concurrency"]["group"]) == "deploy", (
            "deploy.yml must serialize on ONE fixed 'deploy' group so a merge train "
            "coalesces onto the single box instead of racing convergences."
        )

    def test_never_cancels_a_running_convergence(self) -> None:
        cancel = _deploy_workflow()["concurrency"]["cancel-in-progress"]
        assert cancel is False, (
            "cancel-in-progress must be false — a superseding merge must never cancel a "
            "RUNNING convergence (an in-flight worker drain) mid-run."
        )

    def test_deploy_script_fast_forwards_to_latest_main(self) -> None:
        # The fetch/pull pair now lives in deploy/fast-forward-checkout.sh, which
        # wraps it in the lossless-dirt reconciliation (a stray `uv.lock` write
        # aborted the bare `pull --ff-only` on every deploy for 42 commits).
        # Assert on the helper's CODE, not on prose: matching the strings anywhere
        # in deploy.sh would now be satisfied by the comment that points here.
        assert "fast-forward-checkout.sh" in _DEPLOY_SH.read_text(encoding="utf-8")
        code = "\n".join(
            line
            for line in _FF_CHECKOUT_SH.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "fetch --prune origin" in code
        assert "pull --ff-only" in code

    def test_deploy_script_serializes_on_a_host_flock(self) -> None:
        # A remote deploy.sh can outlive its GitHub job, defeating the workflow
        # concurrency group; a host flock is the hard single-convergence backstop
        # so overlapping drains can never strand worker_quiescing ON.
        body = _DEPLOY_SH.read_text(encoding="utf-8")
        assert "flock -n 9" in body, "deploy.sh must take a non-blocking host flock (fd 9)"
        assert "DEPLOY_LOCK" in body
        lock_at = body.find("flock -n 9")
        drain_at = body.find("t3 worker drain")
        assert lock_at != -1
        assert drain_at != -1
        assert lock_at < drain_at, (
            "the flock guard must run BEFORE the worker drain, so a second convergence never starts a competing drain."
        )

    def test_job_timeout_exceeds_the_drain_window(self) -> None:
        # If the GitHub job timeout is below the deploy.sh drain window, GitHub
        # abandons a still-running remote deploy and releases the concurrency
        # group early — the overlap that stranded admission. 1800s == 30 min.
        timeout_minutes = int(_deploy_workflow()["jobs"]["deploy"]["timeout-minutes"])
        assert timeout_minutes > 30, (
            "deploy job timeout-minutes must exceed the 30-min (1800s) drain window plus "
            "build/up/health, or GitHub abandons the in-flight deploy and overlaps runs."
        )


class TestDeployDrain:
    def test_deploy_script_drains_the_running_worker_before_the_swap(self) -> None:
        body = _DEPLOY_SH.read_text(encoding="utf-8")
        drain_at = body.find("t3 worker drain")
        swap_at = body.find("up -d --build")
        assert drain_at != -1, "deploy.sh must drain the worker before swapping the image"
        assert swap_at != -1
        assert drain_at < swap_at, "the drain must run BEFORE `docker compose up -d --build`"
        # Guarded by worker_running (nothing to drain otherwise) and non-fatal on overrun.
        assert "if worker_running; then" in body
        assert "TEATREE_DRAIN_TIMEOUT" in body

    def test_fresh_worker_init_clears_the_quiescing_gate(self) -> None:
        body = _ENTRYPOINT_SH.read_text(encoding="utf-8")
        assert "config_setting set worker_quiescing false" in body, (
            "entrypoint init must CLEAR worker_quiescing (a hard `set false`, not a "
            "provenance `seed`) so the fresh worker resumes admission after a deploy."
        )

    def test_deploy_clears_quiescing_when_stranded_before_the_swap(self) -> None:
        # A run that drains (sets worker_quiescing ON) but dies before the image
        # swap must clear the gate on EXIT so the still-live old worker resumes
        # admission instead of staying quiesced forever.
        body = _DEPLOY_SH.read_text(encoding="utf-8")
        assert "trap _clear_quiescing_if_stranded EXIT" in body
        assert "config_setting set worker_quiescing false" in body, (
            "the stranded-gate fail-safe must clear worker_quiescing on abnormal exit."
        )

    def test_worker_has_a_stop_grace_period(self) -> None:
        compose = yaml.safe_load(_COMPOSE_YML.read_text(encoding="utf-8"))
        assert "stop_grace_period" in compose["services"]["teatree-worker"], (
            "teatree-worker needs a stop_grace_period so a recreate lets the SIGTERM "
            "handler exit cleanly instead of SIGKILL at the 10s default."
        )


class TestDrainSurvivesItsTransport:
    """The long drain outlives its SSH session, and a torn-down one still frees admission (#3983)."""

    def test_the_ssh_transport_is_kept_alive_while_the_drain_waits(self) -> None:
        # Three deploys died 276.8s / 280.0s / ~280s into the drain — a 3s spread is a
        # fixed idle timeout, not a flaky link. Without keepalives the 1800s drain
        # budget is unreachable: any wait on in-flight agents outlives the connection.
        body = _DEPLOY_YML.read_text(encoding="utf-8")
        match = re.search(r"ServerAliveInterval=(\d+)", body)
        assert match is not None, "deploy.yml's ssh invocation must set ServerAliveInterval"
        assert int(match.group(1)) * 2 < _OBSERVED_IDLE_TEARDOWN_SECONDS, (
            "the keepalive cadence must leave room for a missed probe inside the observed idle window"
        )
        assert re.search(r"ServerAliveCountMax=(\d+)", body) is not None

    @pytest.mark.integration
    @pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGPIPE, signal.SIGTERM, signal.SIGINT])
    def test_a_torn_down_session_still_clears_the_stranded_gate(self, tmp_path: Path, sig: int) -> None:
        # A dropped SSH session kills deploy.sh with one of these, mid-drain and long
        # before the swap. Run deploy.sh's REAL fail-safe under each and prove it
        # clears the gate — otherwise the still-live old worker admits nothing.
        calls = _run_fail_safe_under_signal(tmp_path, sig, fail_safe=_fail_safe_block())

        assert any("config_setting set worker_quiescing false" in call for call in calls), (
            f"a deploy killed by {signal.Signals(sig).name} after its drain must free admission; docker calls={calls}"
        )

    @pytest.mark.integration
    def test_the_signal_probe_detects_a_missing_fail_safe(self, tmp_path: Path) -> None:
        # The control for the parametrised probe above: strip the trap and the same
        # harness must record NO clear, so a green there is evidence and not an artifact.
        without_trap = _fail_safe_block().replace(_FAIL_SAFE_END, "")
        calls = _run_fail_safe_under_signal(tmp_path, signal.SIGHUP, fail_safe=without_trap)

        assert calls == []
