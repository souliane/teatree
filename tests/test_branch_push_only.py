"""Branch-scoped pre-push gates skip a fleet claim ref push; every other push still runs them.

prek consumes the pre-push stdin and names the pushed remote ref in
``PRE_COMMIT_REMOTE_BRANCH`` (a claim push reads ``refs/teatree/claims/<slug>``),
which is the contract the wrapper keys on.
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WRAPPER = "scripts/hooks/branch-push-only.sh"
_CONFIG = _ROOT / ".pre-commit-config.yaml"
_LEAK_GATE = "refuse-public-push-with-leak"


def _run_gate(tmp_path: Path, remote_ref: str | None, gate_rc: int) -> tuple[int, bool]:
    ran = tmp_path / "gate-ran"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PRE_COMMIT")}
    if remote_ref is not None:
        env["PRE_COMMIT_REMOTE_BRANCH"] = remote_ref
    gate = ["sh", "-c", f'touch "{ran}"; exit {gate_rc}']
    bash = shutil.which("bash")
    assert bash
    result = subprocess.run([bash, str(_ROOT / _WRAPPER), *gate], env=env, check=False)
    return result.returncode, ran.exists()


def test_a_claim_ref_push_skips_the_gate(tmp_path: Path) -> None:
    assert _run_gate(tmp_path, "refs/teatree/claims/widget-7-0123456789abcdef", gate_rc=1) == (0, False)


@pytest.mark.parametrize("remote_ref", ["refs/heads/feature", "refs/heads/teatree/claims/widget-7", None])
def test_any_other_push_runs_the_gate_and_keeps_its_verdict(tmp_path: Path, remote_ref: str | None) -> None:
    assert _run_gate(tmp_path, remote_ref, gate_rc=3) == (3, True)


def test_every_push_gate_but_the_leak_gate_is_branch_scoped() -> None:
    config = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    default_stages = set(config.get("default_stages", []))
    push_gates = {
        hook["id"]: shlex.split(hook.get("entry", ""))
        for repo in config["repos"]
        for hook in repo["hooks"]
        if set(hook.get("stages", default_stages)) & {"push", "pre-push"}
    }

    unscoped = {hook_id for hook_id, argv in push_gates.items() if argv[:1] != [_WRAPPER]}

    assert unscoped == {_LEAK_GATE}
