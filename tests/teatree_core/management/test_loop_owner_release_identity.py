"""``t3 loop claim`` and ``t3 loop release`` bind the SAME principal (#3810).

The observed defect: a claim printed ``OK claimed loop slot 'loop:inbox' for
this session (47532180-…)`` and the release seconds later printed ``NOOP this
session does not hold loop slot 'loop:inbox'``. The two commands each re-read
the loop registry — a file any concurrent ``SessionStart`` rewrites — so they
could resolve two different identities, and the NOOP named nobody and offered
no way out. A lease could be claimed and never released.
"""

import io
import json
from pathlib import Path

import pytest
from django.core.management import call_command

from teatree.core.models import LoopLease
from teatree.core.session_identity import SESSION_ID_ENV_VARS, runner_identity_env

_SLOT = "loop:inbox"


def _run(*args: str, **kwargs: object) -> str:
    out, err = io.StringIO(), io.StringIO()
    call_command("loop_owner", *args, stdout=out, stderr=err, **kwargs)
    return out.getvalue() + err.getvalue()


def _write_registry(path: Path, session_id: str) -> None:
    path.write_text(
        json.dumps({"t3-loop-tick-owner": {"session_id": session_id, "agent_id": "", "pid": 4_000_001}}),
        encoding="utf-8",
    )


@pytest.fixture
def registry(db: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No session env vars, a rotating loop registry — the runner's real world."""
    for name in SESSION_ID_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("T3_LOOP_SESSION_PID", raising=False)
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir()
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(registry_dir))
    return registry_dir / "loop-registry.json"


class TestClaimReleaseRoundTrip:
    def test_a_runner_claim_is_released_by_the_next_release(
        self, registry: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Claim then release round-trips even when the registry rotates between them."""
        for key, value in runner_identity_env(4_000_002).items():
            monkeypatch.setenv(key, value)
        _write_registry(registry, "aaaaaaaa-0000-0000-0000-000000000000")

        claimed = _run("claim", slot=_SLOT)
        assert "OK    claimed" in claimed, claimed

        _write_registry(registry, "bbbbbbbb-1111-1111-1111-111111111111")
        released = _run("release", slot=_SLOT)

        assert "OK    released" in released, released
        assert LoopLease.objects.get(name=_SLOT).session_id == ""

    def test_a_non_owner_noop_names_the_holder_and_the_way_out(
        self, registry: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused release must be actionable, not a dead end."""
        monkeypatch.setenv("T3_LOOP_SESSION_ID", "somebody-else")
        LoopLease.objects.take_over_ownership(_SLOT, session_id="the-holder", owner_pid=4_000_003)

        output = _run("release", slot=_SLOT)

        assert "the-holder" in output, output
        assert "--force" in output, output

    def test_force_releases_a_lease_no_one_can_present_an_identity_for(
        self, registry: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The operator recovery path for a lease stranded under a departed identity."""
        monkeypatch.setenv("T3_LOOP_SESSION_ID", "somebody-else")
        LoopLease.objects.take_over_ownership(_SLOT, session_id="a-session-that-is-gone", owner_pid=4_000_004)

        output = _run("release", slot=_SLOT, force=True)

        assert "OK    released" in output, output
        assert LoopLease.objects.get(name=_SLOT).session_id == ""
