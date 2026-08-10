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
from typing import NamedTuple

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


def _staged_swap_block(body: str) -> str:
    """deploy.sh's `staged_swap()` body — the shipped stage ORDER, not a copy of it."""
    start = body.find("staged_swap() {")
    assert start != -1, "deploy.sh's staged swap moved — re-anchor this probe"
    end = body.find("\n}\n", start)
    assert end > start, "deploy.sh's staged_swap() is not closed as expected — re-anchor this probe"
    return body[start:end]


def _fail_safe_block() -> str:
    """deploy.sh's stranded-gate fail-safe, verbatim, anchors included."""
    return _slice(_DEPLOY_SH.read_text(encoding="utf-8"), _FAIL_SAFE_START, _FAIL_SAFE_END, "stranded-gate fail-safe")


class _FailSafeRun(NamedTuple):
    calls: list[str]
    stderr: str


def _run_fail_safe_under_signal(
    tmp_path: Path, sig: int, *, fail_safe: str, stage: str = "_DRAINED=true"
) -> _FailSafeRun:
    """Signal a script carrying *fail_safe* mid-run; report its `docker` calls and stderr.

    *stage* is the shipped flag assignment naming how far the convergence got, so a test
    picks the point of death rather than re-implementing the fail-safe's own conditions.
    """
    docker_log = tmp_path / "docker.log"
    stderr_log = tmp_path / "stderr.log"
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
        f'{stage}\ntouch "{ready}"\nsleep 5\n',
        encoding="utf-8",
    )

    env = {**os.environ, "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}"}
    with stderr_log.open("w", encoding="utf-8") as stderr_sink:
        proc = subprocess.Popen(["bash", str(script)], env=env, stdout=subprocess.DEVNULL, stderr=stderr_sink)  # noqa: S607 — a fixture-authored script under tmp_path
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "the harness never reached its drain"
            proc.send_signal(sig)
            proc.wait(timeout=10)
        finally:
            proc.kill()
    return _FailSafeRun(
        calls=docker_log.read_text(encoding="utf-8").splitlines() if docker_log.exists() else [],
        stderr=stderr_log.read_text(encoding="utf-8"),
    )


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
        staged = _staged_swap_block(body)
        drains = [m.start() for m in re.finditer(r"^\s+drain_worker$", staged, re.MULTILINE)]
        # init clears worker_quiescing as its last act, so the gate is asserted once
        # before migrations and re-asserted between init and the worker's recreate.
        assert len(drains) == 2, f"the staged swap must drain either side of init; found {len(drains)}"
        assert drains[0] < staged.index("up -d --no-deps teatree-init")
        assert staged.index("up -d --no-deps teatree-init") < drains[1] < staged.index("up -d --no-deps teatree-worker")
        # Guarded by worker_running (nothing to drain otherwise) and non-fatal on overrun.
        assert "worker_running || return 0" in body
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
        run = _run_fail_safe_under_signal(tmp_path, sig, fail_safe=_fail_safe_block())

        assert any("config_setting set worker_quiescing false" in call for call in run.calls), (
            f"a deploy killed by {signal.Signals(sig).name} after its drain must free admission; calls={run.calls}"
        )

    @pytest.mark.integration
    def test_the_signal_probe_detects_a_missing_fail_safe(self, tmp_path: Path) -> None:
        # The control for the parametrised probe above: strip the trap and the same
        # harness must record NO clear, so a green there is evidence and not an artifact.
        without_trap = _fail_safe_block().replace(_FAIL_SAFE_END, "")
        run = _run_fail_safe_under_signal(tmp_path, signal.SIGHUP, fail_safe=without_trap)

        assert run.calls == []


class TestAStrandedConvergenceFailsTowardsTheSaferSide:
    """Which way the fail-safe fails depends on what the still-live worker would run (#4214)."""

    @pytest.mark.integration
    def test_a_strand_after_init_leaves_admission_closed_rather_than_admitting_on_a_migrated_db(
        self, tmp_path: Path
    ) -> None:
        # init applies migrations at stage 3, so a convergence that dies between it and
        # the worker swap leaves the OLD worker — pre-migration code — live against the
        # NEW schema. Re-opening admission there hands it fresh work to run against a
        # database it does not match; staying quiesced only stalls, which is visible and
        # recoverable. Fail towards the stall.
        run = _run_fail_safe_under_signal(
            tmp_path,
            signal.SIGTERM,
            fail_safe=_fail_safe_block(),
            stage="_DRAINED=true\n_INIT_RAN=true",
        )

        assert not any("config_setting set worker_quiescing false" in call for call in run.calls), (
            f"admission must stay closed on the old worker once init has migrated the DB; docker calls={run.calls}"
        )
        assert "worker_quiescing" in run.stderr, "the refusal must name the gate it is deliberately leaving ON"

    def test_the_convergence_records_each_stage_the_fail_safe_branches_on(self) -> None:
        # The trap reads two flags; both are inert unless the shipped stages set them,
        # and an inert fail-safe silently reverts to always-clear. Pin each assignment
        # to the stage whose completion it claims.
        staged = _staged_swap_block(_DEPLOY_SH.read_text(encoding="utf-8"))

        init_ran_at = staged.index("_INIT_RAN=true")
        assert staged.index("wait_for_init") < init_ran_at < staged.index("up -d --no-deps teatree-worker"), (
            "_INIT_RAN must be set once init has migrated and while the OLD worker is still the live one"
        )
        assert staged.index("up -d --no-deps teatree-worker") < staged.index("_WORKER_SWAPPED=true"), (
            "_WORKER_SWAPPED must be set only once the fresh worker has actually been created"
        )
        assert staged.index("_WORKER_SWAPPED=true") < staged.index("resume_admission"), (
            "resume_admission's own failure must leave the trap able to retry the clear"
        )

    @pytest.mark.integration
    def test_a_strand_after_the_worker_swap_still_retries_the_clear(self, tmp_path: Path) -> None:
        # Past the swap the live worker is the FRESH one, which matches the migrated
        # schema — so the trap's retry of a failed `resume_admission` must survive the
        # refusal above rather than be caught by it.
        run = _run_fail_safe_under_signal(
            tmp_path,
            signal.SIGTERM,
            fail_safe=_fail_safe_block(),
            stage="_DRAINED=true\n_INIT_RAN=true\n_WORKER_SWAPPED=true",
        )

        assert any("config_setting set worker_quiescing false" in call for call in run.calls), (
            f"a fresh worker must still have admission restored; docker calls={run.calls}"
        )
