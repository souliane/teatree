"""The credential is verified BEFORE the checkout that consumes it (#4262).

``actions/checkout`` installs ``TEATREE_GH_TOKEN`` as the git credential. When the forge
rejects it the fetch comes back unauthorized, git falls back to interactive prompting, and
the job dies with ``could not read Username for 'https://github.com'`` — the byte-identical
message an *unset* secret produces. Both workflows carried a guard for exactly this, three
steps downstream in the PR step, so it was structurally unreachable; and it tested presence,
which was never the failure mode. The credential died between 2026-07-19 and 2026-07-26 and
nothing named it for three weeks.

So the probe runs first, and its wording covers both cases. It is executed here against a
stubbed forge rather than asserted as prose: a guard nobody has run with a set-but-invalid
value is a guard nobody has tested.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_BASH = shutil.which("bash") or "/bin/bash"

#: (workflow file, job key) for every consumer that checks out with the PAT.
PAT_CONSUMERS = [("ci.yml", "refresh-durations"), ("uv-lock-upgrade.yml", "refresh-lockfile")]

_PROBE_NAME = "Verify TEATREE_GH_TOKEN"
_BOTH_CASES = "unset or rejected"


def _steps(workflow: str, job: str) -> list[dict[str, Any]]:
    loaded = cast("dict[str, Any]", yaml.safe_load((_WORKFLOWS / workflow).read_text(encoding="utf-8")))
    return [step for step in loaded["jobs"][job].get("steps", []) if isinstance(step, dict)]


def _probe_step(workflow: str, job: str) -> dict[str, Any]:
    matches = [step for step in _steps(workflow, job) if _PROBE_NAME in str(step.get("name", ""))]
    assert matches, (
        f"{workflow}:{job} has no step named '{_PROBE_NAME}…'. The credential must be probed "
        "ahead of the checkout that consumes it, or a rejected one dies in a generic git prompt "
        "error that names nothing (#4262)."
    )
    return matches[0]


def _probe_script(workflow: str, job: str) -> str:
    return str(_probe_step(workflow, job).get("run", ""))


def _run_probe(
    script: str,
    tmp_path: Path,
    *,
    credential: str | None = "a-credential",
    http_status: str = "200",
    curl_exit: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Execute the workflow's own probe script against a stubbed forge."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "curl"
    stub.write_text(f"#!/bin/sh\nprintf '%s' '{http_status}'\nexit {curl_exit}\n", encoding="utf-8")
    stub.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GITHUB_REPOSITORY": "souliane/teatree",
    }
    if credential is not None:
        env["TEATREE_GH_TOKEN"] = credential
    return subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )


@pytest.mark.parametrize(("workflow", "job"), PAT_CONSUMERS)
class TestTheProbeRunsBeforeCheckout:
    def test_it_precedes_every_checkout_in_the_job(self, workflow: str, job: str) -> None:
        steps = _steps(workflow, job)
        probe = steps.index(_probe_step(workflow, job))
        checkouts = [i for i, step in enumerate(steps) if "actions/checkout" in str(step.get("uses", ""))]
        assert checkouts, f"{workflow}:{job} has no actions/checkout step to guard."
        assert probe < min(checkouts), (
            f"{workflow}:{job} probes the credential at step {probe}, after the checkout at step "
            f"{min(checkouts)} that consumes it. A guard downstream of the failure it explains "
            "never prints — the job is already dead (#4262)."
        )

    def test_the_probe_reads_the_secret_it_guards(self, workflow: str, job: str) -> None:
        env = cast("dict[str, Any]", _probe_step(workflow, job).get("env", {}))
        assert "TEATREE_GH_TOKEN" in str(env.get("TEATREE_GH_TOKEN", "")), (
            f"{workflow}:{job}'s probe must receive secrets.TEATREE_GH_TOKEN — the credential the "
            "checkout below persists is the one that has to be verified."
        )


@pytest.mark.parametrize(("workflow", "job"), PAT_CONSUMERS)
class TestTheProbeVerdicts:
    def test_a_rejected_token_fails_loud_and_names_both_cases(self, workflow: str, job: str, tmp_path: Path) -> None:
        result = _run_probe(_probe_script(workflow, job), tmp_path, credential="stale-pat", http_status="401")
        assert result.returncode != 0, (
            f"{workflow}:{job}'s probe accepted a token the forge answered 401 for. Presence was "
            "never the failure mode — validity is (#4262)."
        )
        assert "::error::" in result.stderr, "a rejected credential must be a GitHub error annotation."
        assert _BOTH_CASES in result.stderr, (
            "the message must cover BOTH cases — a reader sent to 'the secret is unset' when the "
            "secret is set and stale is sent to fix something that is not broken."
        )
        assert "401" in result.stderr, "the message must name the status the forge actually answered."

    def test_an_unset_token_fails_loud_with_the_same_wording(self, workflow: str, job: str, tmp_path: Path) -> None:
        result = _run_probe(_probe_script(workflow, job), tmp_path, credential=None)
        assert result.returncode != 0, f"{workflow}:{job}'s probe accepted an absent credential."
        assert _BOTH_CASES in result.stderr

    def test_an_accepted_token_passes_quietly(self, workflow: str, job: str, tmp_path: Path) -> None:
        result = _run_probe(_probe_script(workflow, job), tmp_path, http_status="200")
        assert result.returncode == 0, (
            f"{workflow}:{job}'s probe rejected a credential the forge accepted:\n{result.stderr}"
        )
        assert "::error::" not in result.stderr

    def test_an_unreachable_forge_is_unverified_not_a_rejection(self, workflow: str, job: str, tmp_path: Path) -> None:
        result = _run_probe(_probe_script(workflow, job), tmp_path, http_status="000", curl_exit=6)
        assert result.returncode == 0, (
            f"{workflow}:{job}'s probe turned an unreachable forge into a credential rejection. The "
            "probe explains a failure; it must not invent one — the checkout below is the authority."
        )
        assert "UNVERIFIED" in result.stderr, (
            "a probe that could not measure must say so — silence is indistinguishable from a pass."
        )
        assert "::warning::" in result.stderr


class TestTheProbeIsOneGuardNotTwo:
    def test_both_consumers_carry_a_byte_identical_probe(self) -> None:
        scripts = {f"{workflow}:{job}": _probe_script(workflow, job) for workflow, job in PAT_CONSUMERS}
        assert len(set(scripts.values())) == 1, (
            "the two PAT consumers' probes have drifted apart. One dead credential takes down both "
            "pipelines, so both must report it identically:\n  " + "\n  ".join(scripts)
        )

    def test_the_credential_never_reaches_the_process_table(self) -> None:
        script = _probe_script(*PAT_CONSUMERS[0])
        assert "--config" in script, (
            "curl must read the Authorization header from a config file, not from argv — a token "
            "passed as an argument is readable by every process on the runner."
        )
        assert "Authorization" not in script.replace('header = "Authorization: Bearer', ""), (
            "the only Authorization header may be the one written into curl's config file."
        )
