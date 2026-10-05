"""A claim push runs the clone's real public-repo privacy gate: the claim identity passes it, the pre-fix one does not.

The gate script is installed as the client's native ``pre-push`` hook, pinned to a
PUBLIC verdict, and scans with the real privacy scanner.
"""

from pathlib import Path

import pytest

from teatree.core.fleet import claim as fleet_claim

from ._git_origin import init_bare, init_client

_ROOT = Path(__file__).resolve().parents[3]
_LEAK_GATE = _ROOT / "scripts" / "hooks" / "refuse-public-push-with-leak.sh"
_SCANNER = _ROOT / "scripts" / "privacy_scan.py"
_WORK_KEY = "https://github.com/acme/widget/issues/7"
_PRE_FIX_EMAIL = "fleet-claim@teatree.local"  # privacy-scan:allow the claim identity before the noreply fix


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
    assert fleet_claim.acquire(_WORK_KEY, repo=str(gated_client)) is not None


def test_the_gate_refuses_a_claim_authored_under_the_pre_fix_identity(gated_client: Path) -> None:
    pre_fix_identity = {"GIT_AUTHOR_EMAIL": _PRE_FIX_EMAIL, "GIT_COMMITTER_EMAIL": _PRE_FIX_EMAIL}

    with pytest.raises(fleet_claim.FleetClaimUnavailableError, match="non-noreply commit identity"):
        fleet_claim.acquire(_WORK_KEY, repo=str(gated_client), extra_env=pre_fix_identity)
