"""Tests for ``teatree.core.session_identity`` (#1107 Prong A).

The headline #1107 root cause: Claude Code delivers the session id only in
the hook JSON payload, NOT as an env var inside Bash-tool subprocesses, so
``current_session_id()`` returned ``""`` in agent-driven mode → ``t3 loop
claim`` hard-refused → t3-master could never be claimed → every
owner-gated slot was permanently dead (the 131-DM incident).

The fix adds a third, lowest-precedence fallback: read the loop-registry
file's owner record. These tests pin the precedence and the fail-open
branches.
"""

import io
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from teatree.core.models import LoopLease
from teatree.core.session_identity import current_session_id, current_session_pid

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _no_session_env() -> dict[str, str]:
    import os  # noqa: PLC0415

    return {k: v for k, v in os.environ.items() if k not in {"CLAUDE_SESSION_ID", "T3_LOOP_SESSION_ID"}}


class TestSessionIdRegistryFallback:
    def test_current_session_id_reads_loop_registry_when_env_absent(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "sess-abc"}}), encoding="utf-8"
        )
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_id() == "sess-abc"

    def test_env_var_takes_precedence_over_registry(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "sess-abc"}}), encoding="utf-8"
        )
        with patch.dict(
            "os.environ",
            {**_no_session_env(), "CLAUDE_SESSION_ID": "foo", "T3_LOOP_REGISTRY_DIR": str(tmp_path)},
            clear=True,
        ):
            assert current_session_id() == "foo"

    def test_missing_registry_file_is_empty(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_id() == ""

    def test_corrupt_registry_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text("{not valid json", encoding="utf-8")
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_id() == ""

    def test_non_dict_owner_record_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(json.dumps({"t3-loop-tick-owner": "not-a-dict"}), encoding="utf-8")
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_id() == ""

    def test_home_unresolvable_falls_back_to_empty(self) -> None:
        """``Path.home()`` raising ``RuntimeError`` must NOT crash claim resolution.

        Seen in CI sandboxes that ``clear=True`` the environment so neither
        ``HOME``, ``XDG_DATA_HOME``, nor ``T3_LOOP_REGISTRY_DIR`` is set —
        ``Path.home()`` then raises. Observed live in the pre-push hook
        on Linux aarch64 (#1107). The fail-open must absorb ``RuntimeError``
        too, not just ``OSError``/``ValueError``.
        """
        from pathlib import Path as _PathRef  # noqa: PLC0415

        def _boom() -> _PathRef:
            msg = "Could not determine home directory."
            raise RuntimeError(msg)

        env = {k: v for k, v in _no_session_env().items() if k != "T3_LOOP_REGISTRY_DIR"}
        env = {k: v for k, v in env.items() if k != "XDG_DATA_HOME"}
        with patch.dict("os.environ", env, clear=True), patch.object(_PathRef, "home", _boom):
            assert current_session_id() == ""

    def test_xdg_data_home_resolution_when_no_registry_dir_env(self, tmp_path: Path) -> None:
        teatree_dir = tmp_path / "teatree"
        teatree_dir.mkdir()
        (teatree_dir / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "xdg-sess"}}), encoding="utf-8"
        )
        env = {k: v for k, v in _no_session_env().items() if k != "T3_LOOP_REGISTRY_DIR"}
        with patch.dict("os.environ", {**env, "XDG_DATA_HOME": str(tmp_path)}, clear=True):
            assert current_session_id() == "xdg-sess"


class TestCurrentSessionPid:
    """The durable owning-session pid for the t3-master lease anchor (#1706).

    The lease ``owner_pid`` must be the long-lived session process, not
    ``os.getppid()`` of the transient Bash-tool tick subprocess. The
    SessionStart hook already records that durable pid in the same loop
    registry record this resolver reads.
    """

    def test_reads_pid_from_registry_owner_record(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "s", "pid": 4242}}), encoding="utf-8"
        )
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_pid() == 4242

    def test_string_pid_is_coerced(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "s", "pid": "4242"}}), encoding="utf-8"
        )
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_pid() == 4242

    def test_missing_registry_is_none(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_pid() is None

    def test_missing_pid_field_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "s"}}), encoding="utf-8"
        )
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_pid() is None

    def test_non_numeric_pid_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "s", "pid": "not-a-pid"}}), encoding="utf-8"
        )
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_pid() is None

    def test_corrupt_registry_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text("{not json", encoding="utf-8")
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_pid() is None


class TestCurrentSessionPidEnvFallback:
    """The env-var precedence the registry-only resolver lacked (#1722).

    A self-pumped tick runs in an env-restricted Bash-tool subprocess: the
    loop registry can be unreadable (``T3_LOOP_REGISTRY_DIR`` points
    nowhere), but the Stop self-pump exports ``T3_LOOP_SESSION_PID``. With
    only the registry source the resolver returned ``None`` and the tick
    silently anchored the lease on ``os.getppid()`` of the transient shell,
    collapsing pid-liveness to TTL-only. The env path must resolve the
    durable pid even with the registry invisible.
    """

    def _no_registry_env(self, tmp_path: Path) -> dict[str, str]:
        return {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path / "does-not-exist")}

    def test_env_pid_resolves_when_registry_unreadable(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {**self._no_registry_env(tmp_path), "T3_LOOP_SESSION_PID": "4242"}, clear=True):
            assert current_session_pid() == 4242

    def test_env_pid_takes_precedence_over_registry(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "s", "pid": 111}}), encoding="utf-8"
        )
        with patch.dict(
            "os.environ",
            {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path), "T3_LOOP_SESSION_PID": "4242"},
            clear=True,
        ):
            assert current_session_pid() == 4242

    def test_no_env_and_no_registry_is_none(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", self._no_registry_env(tmp_path), clear=True):
            assert current_session_pid() is None

    def test_non_numeric_env_pid_falls_back_to_registry(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "s", "pid": 111}}), encoding="utf-8"
        )
        with patch.dict(
            "os.environ",
            {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path), "T3_LOOP_SESSION_PID": "not-a-pid"},
            clear=True,
        ):
            assert current_session_pid() == 111


class TestLoopClaimSucceedsViaRegistrySessionId:
    """The literal #1107 incident reproduction (Prong A2)."""

    def test_loop_claim_succeeds_via_registry_session_id(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "sess-abc"}}), encoding="utf-8"
        )
        out = io.StringIO()
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            call_command("loop_owner", "claim", "--take-over", stdout=out)

        assert "OK    claimed" in out.getvalue()
        status = LoopLease.objects.ownership_status("t3-master")
        assert status.is_live is True
        assert status.owner_session == "sess-abc"

    def test_take_over_anchors_lease_on_durable_session_pid(self, tmp_path: Path) -> None:
        """``t3 loop claim --take-over`` must store the durable session pid (#1706).

        The command runs in a Bash-tool shell torn down seconds later, so
        ``os.getppid()`` there is a transient pid. Anchoring the lease on it
        made the take-over "only hold until the next fresh session" — the
        new session saw a dead pid + lapsed TTL and stole the loop. The
        lease must instead carry the durable session pid from the registry.
        """
        import os  # noqa: PLC0415

        durable_session_pid = os.getpid()
        (tmp_path / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "sess-abc", "pid": durable_session_pid}}),
            encoding="utf-8",
        )
        out = io.StringIO()
        with (
            patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True),
            patch("os.getppid", return_value=999999),
        ):
            call_command("loop_owner", "claim", "--take-over", stdout=out)

        row = LoopLease.objects.get(name="t3-master")
        assert row.owner_pid == durable_session_pid, (
            "take-over must anchor on the durable session pid, not os.getppid() of the transient shell"
        )


class TestEnvInvisibleRegistryAnchorsDurablePid:
    """The #1722 gap: env-restricted subprocess with the registry unreadable.

    The Stop self-pump exports both ``T3_LOOP_SESSION_ID`` and
    ``T3_LOOP_SESSION_PID`` into the tick command. When that tick runs in a
    subprocess that cannot read the loop registry, the env-propagated pid is
    the ONLY durable source. Before the fix ``current_session_pid()``
    returned ``None`` there and the claim fell back to ``os.getppid()`` of
    the torn-down shell — collapsing pid-liveness to TTL-only. These tests
    fail on that pre-fix behaviour (lease anchors on the transient pid; a
    fresh session steals a past-TTL owner) and pass once the env pid is
    resolved.
    """

    def _unreadable_registry_env(self, tmp_path: Path, durable_session_pid: int) -> dict[str, str]:
        return {
            **_no_session_env(),
            "T3_LOOP_REGISTRY_DIR": str(tmp_path / "does-not-exist"),
            "T3_LOOP_SESSION_ID": "owner-sess",
            "T3_LOOP_SESSION_PID": str(durable_session_pid),
        }

    def test_claim_anchors_on_env_pid_when_registry_unreadable(self, tmp_path: Path) -> None:
        import os  # noqa: PLC0415

        durable_session_pid = os.getpid()
        env = self._unreadable_registry_env(tmp_path, durable_session_pid)
        out = io.StringIO()
        with (
            patch.dict("os.environ", env, clear=True),
            patch("os.getppid", return_value=999999),
        ):
            call_command("loop_owner", "claim", "--take-over", stdout=out)

        row = LoopLease.objects.get(name="t3-master")
        assert row.owner_pid == durable_session_pid, (
            "with the registry unreadable, the lease must anchor on the env-propagated durable "
            "session pid, never os.getppid() of the transient tick shell"
        )

    def test_alive_owner_past_ttl_is_not_stealable(self, tmp_path: Path) -> None:
        import os  # noqa: PLC0415

        durable_session_pid = os.getpid()
        env = self._unreadable_registry_env(tmp_path, durable_session_pid)
        out = io.StringIO()
        with (
            patch.dict("os.environ", env, clear=True),
            patch("os.getppid", return_value=999999),
        ):
            call_command("loop_owner", "claim", "--take-over", stdout=out)

        row = LoopLease.objects.get(name="t3-master")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])

        won, owner = LoopLease.objects.claim_ownership("t3-master", session_id="fresh-session")
        assert won is False, "an alive owner past its TTL must NOT be stealable by a fresh session"
        assert owner == "owner-sess"
