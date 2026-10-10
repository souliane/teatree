"""A claim push runs the clone's real public-repo privacy gate: the claim identity passes it, the pre-fix one does not.

The gate script is installed as the client's native ``pre-push`` hook and scans with the real
privacy scanner of its own checkout. Its remote is a local path no forge can classify, so the gate
treats it as it treats a public one: nothing proves it private.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from teatree.core.fleet import claim as fleet_claim
from tests._leak_hook_checkout import place_hook

from ._git_origin import init_bare, init_client

_REGISTRY = json.dumps({"leak": ["demo-leak-term"]})
_WORK_KEY = "https://github.com/acme/widget/issues/7"
_PRE_FIX_EMAIL = "fleet-claim@teatree.local"  # privacy-scan:allow the claim identity before the noreply fix


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
