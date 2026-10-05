"""A claim push runs the clone's real public-repo privacy gate, and the claim commit passes it.

The gate script is installed as the client's native ``pre-push`` hook, pinned to a
PUBLIC verdict, and scans with the real privacy scanner.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from teatree.core.fleet import claim as fleet_claim

from ._git_origin import git, init_bare, init_client

_ROOT = Path(__file__).resolve().parents[3]
_LEAK_GATE = _ROOT / "scripts" / "hooks" / "refuse-public-push-with-leak.sh"
_SCANNER = _ROOT / "scripts" / "privacy_scan.py"
_REAL_EMAIL = "real.dev@internal.example"  # privacy-scan:allow fixture


@pytest.fixture
def gated_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    client = init_client(tmp_path / "client", init_bare(tmp_path / "origin.git"))
    pre_push = client / ".git" / "hooks" / "pre-push"
    pre_push.write_text(f'#!/bin/sh\nexec bash "{_LEAK_GATE}" "$@"\n', encoding="utf-8")
    pre_push.chmod(0o755)
    verdict = tmp_path / "visibility-public"
    verdict.write_text("#!/bin/sh\necho PUBLIC\n", encoding="utf-8")
    verdict.chmod(0o755)
    monkeypatch.setenv("T3_REPO_VISIBILITY_CMD", str(verdict))
    monkeypatch.setenv("T3_PRIVACY_SCAN_CMD", f"python3 {_SCANNER}")
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "hook-state"))
    return client


def test_a_fleet_claim_passes_the_gate_on_a_public_remote(gated_client: Path) -> None:
    assert fleet_claim.acquire("https://github.com/acme/widget/issues/7", repo=str(gated_client)) is not None


def test_the_gate_still_refuses_a_real_identity_pushed_to_a_claim_ref(gated_client: Path) -> None:
    tree = git(gated_client, "mktree")
    sha = git(
        gated_client, "-c", "user.name=Real Dev", "-c", f"user.email={_REAL_EMAIL}", "commit-tree", tree, "-m", "{}"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    git_bin = shutil.which("git")
    assert git_bin

    pushed = subprocess.run(
        [git_bin, "-C", str(gated_client), "push", "origin", f"{sha}:refs/teatree/claims/widget-7"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert pushed.returncode != 0
    assert _REAL_EMAIL in pushed.stdout + pushed.stderr
