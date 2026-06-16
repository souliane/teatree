"""Tests for ``t3 <overlay> handover`` and ``t3 loop whoami`` commands."""

import json
import os
import pathlib
from io import StringIO

import pytest
from django.core.management import call_command

from teatree.core.models import LoopLease, SessionHandover

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _call(*args: str, **kwargs) -> str:
    buf = StringIO()
    call_command(*args, stdout=buf, **kwargs)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _session(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Pin this 'session' id and isolate the snapshot dir + XDG mirror."""
    monkeypatch.setenv("T3_LOOP_SESSION_ID", "this-session")
    monkeypatch.setenv("TEATREE_CLAUDE_STATUSLINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)


class TestHandoverCreate:
    def test_no_target_parks_for_next_when_no_owner(self) -> None:
        out = _call("handover", "create", json_output=True)
        data = json.loads(out)
        assert data["ok"] is True
        assert data["parked_for_next"] is True
        row = SessionHandover.objects.get()
        assert row.from_session == "this-session"
        assert row.to_session == ""

    def test_no_target_hands_to_live_loop_owner(self) -> None:
        LoopLease.objects.claim_ownership("loop-owner", session_id="owner-X", owner_pid=os.getpid())
        out = _call("handover", "create", json_output=True)
        data = json.loads(out)
        assert data["to_session"] == "owner-X"
        assert data["parked_for_next"] is False

    def test_explicit_target(self) -> None:
        out = _call("handover", "create", to="target-Z", json_output=True)
        assert json.loads(out)["to_session"] == "target-Z"

    def test_create_writes_xdg_mirror(self) -> None:
        data = json.loads(_call("handover", "create", json_output=True))
        mirror = data["mirror_path"]
        assert "handover" in mirror
        assert pathlib.Path(mirror).is_file()

    def test_no_session_id_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("T3_LOOP_SESSION_ID", raising=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", "/nonexistent-registry-dir")
        with pytest.raises(SystemExit):
            _call("handover", "create", json_output=True)


class TestHandoverWhoami:
    def test_whoami_prints_session_id(self) -> None:
        assert _call("handover", "whoami").strip() == "this-session"

    def test_whoami_json(self) -> None:
        assert json.loads(_call("handover", "whoami", json_output=True))["session_id"] == "this-session"


class TestHandoverClaimOnStart:
    def test_claims_handover_targeted_at_session(self) -> None:
        SessionHandover.objects.create_handover(from_session="other", to_session="this-session", payload="BODY")
        out = _call("handover", "claim-on-start", session="this-session", json_output=True)
        data = json.loads(out)
        assert data["claimed"] is True
        assert data["payload"] == "BODY"
        assert data["from_session"] == "other"
        assert SessionHandover.objects.get().claimed_by == "this-session"

    def test_claim_is_idempotent_single_use(self) -> None:
        SessionHandover.objects.create_handover(from_session="other", to_session="this-session", payload="BODY")
        assert json.loads(_call("handover", "claim-on-start", session="this-session", json_output=True))["claimed"]
        assert not json.loads(_call("handover", "claim-on-start", session="this-session", json_output=True))["claimed"]

    def test_nothing_to_claim(self) -> None:
        assert json.loads(_call("handover", "claim-on-start", session="fresh", json_output=True))["claimed"] is False


class TestLoopWhoamiAndOwnerDisplay:
    def test_loop_owner_whoami_prints_session_id(self) -> None:
        assert _call("loop_owner", "whoami").strip() == "this-session"

    def test_loop_owner_shows_you_are(self) -> None:
        out = _call("loop_owner", "owner")
        assert "you are: this-session" in out

    def test_loop_owner_json_includes_you_and_owner_flag(self) -> None:
        LoopLease.objects.claim_ownership("loop-owner", session_id="this-session", owner_pid=os.getpid())
        data = json.loads(_call("loop_owner", "owner", json_output=True))
        assert data["you"] == "this-session"
        assert data["you_are_owner"] is True
