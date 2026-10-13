"""A claim push runs the clone's real public-repo privacy gate: the claim identity passes it, the pre-fix one does not.

The gate script is installed as the client's native ``pre-push`` hook and scans with the real
privacy scanner of its own checkout. Its remote is a local path no forge can classify, so the gate
treats it as it treats a public one: nothing proves it private.

A root-continuity ``pre-push`` hook, which refuses any pushed commit whose history has a root other
than the default branch's, passes every claim commit too.
"""

import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from teatree.core.fleet import claim as fleet_claim
from tests._leak_hook_checkout import place_hook

from ._git_origin import FIXTURE_IDENTITY, as_other_instance, empty_tree, git, init_bare, init_client, ref_sha

_REGISTRY = json.dumps({"leak": ["demo-leak-term"]})
_WORK_KEY = "https://github.com/acme/widget/issues/7"
_PRE_FIX_EMAIL = "fleet-claim@teatree.local"  # privacy-scan:allow the claim identity before the noreply fix

_ROOT_CONTINUITY_HOOK = """#!/bin/sh
expected=$(git rev-list --max-parents=0 refs/remotes/origin/main) || exit 1
while read -r lref lsha rref rsha; do
    [ "$lsha" = "0000000000000000000000000000000000000000" ] && continue
    roots=$(git rev-list --max-parents=0 "$lsha") || exit 1
    [ "$roots" = "$expected" ] || { echo "root-continuity: $lsha has root $roots, not $expected" >&2; exit 1; }
done
exit 0
"""


def _with_root_continuity_hook(client: Path) -> Path:
    hook = client / ".git" / "hooks" / "pre-push"
    hook.write_text(_ROOT_CONTINUITY_HOOK, encoding="utf-8")
    hook.chmod(0o755)
    return client


def _parentless_commit(repo: Path) -> str:
    return git(repo, *FIXTURE_IDENTITY, "commit-tree", empty_tree(repo), "-m", "parentless")


def test_every_claim_push_passes_a_root_continuity_pre_push_hook(tmp_path: Path) -> None:
    bare = init_bare(tmp_path / "origin.git")
    holder = _with_root_continuity_hook(init_client(tmp_path / "holder", bare))
    with pytest.raises(subprocess.CalledProcessError) as control:
        git(holder, "push", "origin", f"{_parentless_commit(holder)}:refs/teatree/claims/control")
    assert "root-continuity" in control.value.stderr

    held = fleet_claim.acquire(_WORK_KEY, repo=str(holder), ttl_seconds=10.0, now=1000.0)
    assert held is not None
    beat = fleet_claim.heartbeat(held, repo=str(holder), now=1001.0)
    assert isinstance(beat, fleet_claim.Claim)
    with as_other_instance(tmp_path / "rival-data"):
        rival = _with_root_continuity_hook(init_client(tmp_path / "rival", bare))
        stolen = fleet_claim.steal_if_expired(_WORK_KEY, repo=str(rival), ttl_seconds=10.0, now=5000.0)
    assert stolen is not None
    legacy_tip = _parentless_commit(bare)
    git(bare, "update-ref", stolen.ref, legacy_tip)
    healed = fleet_claim.heartbeat(replace(stolen, sha=legacy_tip), repo=str(rival), now=5001.0)

    assert isinstance(healed, fleet_claim.Claim)
    assert ref_sha(bare, stolen.ref) == healed.sha


def _config_db_without_private_repos(db: Path) -> Path:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'private_repos', '[]')")
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.fixture
def gated_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    client = init_client(tmp_path / "client", init_bare(tmp_path / "origin.git"))
    leak_gate = place_hook(tmp_path / "gate-checkout", registry=_REGISTRY)
    pre_push = client / ".git" / "hooks" / "pre-push"
    pre_push.write_text(f'#!/bin/sh\nexec bash "{leak_gate}" "$@"\n', encoding="utf-8")
    pre_push.chmod(0o755)
    monkeypatch.setenv("TEATREE_TERM_REGISTRY", _REGISTRY)
    monkeypatch.setenv("T3_CONFIG_DB", str(_config_db_without_private_repos(tmp_path / "config.sqlite3")))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "hook-state"))
    return client


def test_a_fleet_claim_passes_the_gate_on_a_remote_nothing_proves_private(gated_client: Path) -> None:
    assert fleet_claim.acquire(_WORK_KEY, repo=str(gated_client)) is not None


def test_the_gate_refuses_a_claim_authored_under_the_pre_fix_identity(gated_client: Path) -> None:
    pre_fix_identity = {"GIT_AUTHOR_EMAIL": _PRE_FIX_EMAIL, "GIT_COMMITTER_EMAIL": _PRE_FIX_EMAIL}

    with pytest.raises(fleet_claim.FleetClaimUnavailableError, match="non-noreply commit identity"):
        fleet_claim.acquire(_WORK_KEY, repo=str(gated_client), extra_env=pre_fix_identity)
