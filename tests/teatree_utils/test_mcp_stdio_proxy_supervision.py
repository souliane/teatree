"""The MCP stdio proxy keeps a client connected while its real server child dies and returns."""

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import types as mcp_types

from teatree.utils.mcp_stdio_proxy import StdioProxy, Timing
from tests._process_liveness import is_gone_within
from tests.teatree_utils._mcp_proxy_children import (
    SDK_SERVER,
    FakeChild,
    Harness,
    request,
    retry_reason,
    sdk_call,
    sdk_discover,
    sdk_initialize,
    sdk_initialized,
    sdk_listen,
    tool_pid,
    write_child,
)

pytestmark = pytest.mark.integration

INITIALIZED = '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'


@pytest.fixture
def sdk_argv(tmp_path: Path) -> list[str]:
    return [sys.executable, str(write_child(tmp_path, "sdk_server.py", SDK_SERVER))]


def fake_call(request_id: object) -> dict[str, Any]:
    return request(request_id, "tools/call", {})


def is_listen_ack(listen_id: str) -> Callable[[dict[str, Any]], bool]:
    def matches(frame: dict[str, Any]) -> bool:
        meta = frame.get("params", {}).get("_meta", {})
        return frame.get("method") == "notifications/subscriptions/acknowledged" and (
            meta.get("io.modelcontextprotocol/subscriptionId") == listen_id
        )

    return matches


def has_id(request_id: object) -> Callable[[dict[str, Any]], bool]:
    return lambda frame: frame.get("id") == request_id


def wait_until_down(harness: Harness) -> None:
    for attempt in range(200):
        request_id = f"down-{attempt}"
        harness.send(request(request_id, "tools/list", {}))
        if retry_reason(harness.recv_matching(has_id(request_id))) == "server_restarting":
            return
        time.sleep(0.05)
    pytest.fail("the proxy never reported the server as restarting")


def negotiate(harness: Harness) -> None:
    harness.send(sdk_discover("d0"))
    harness.recv_matching(has_id("d0"))


class TestRecoveryAcrossAServerDeath:
    def test_a_killed_server_is_replaced_and_the_next_call_is_served_by_the_new_one(
        self, sdk_argv: list[str], capfd: pytest.CaptureFixture[str]
    ) -> None:
        harness = Harness(sdk_argv)
        try:
            harness.send(sdk_discover("d1"))
            harness.recv_matching(has_id("d1"))
            harness.send(sdk_call(1, "echo"))
            first = tool_pid(harness.recv_matching(has_id(1)))

            os.kill(first, signal.SIGKILL)
            served, refused = harness.call_until_served(lambda rid: sdk_call(rid, "echo"))

            assert tool_pid(served) != first
            assert all(retry_reason(frame) for frame in refused)
            assert "mcp-stdio-proxy: restarting the server" in capfd.readouterr().err
        finally:
            harness.close()

    def test_a_call_in_flight_at_the_death_is_told_it_may_have_run(self, sdk_argv: list[str]) -> None:
        harness = Harness(sdk_argv)
        try:
            harness.send(sdk_discover("d1"))
            harness.recv_matching(has_id("d1"))
            harness.send(sdk_call(1, "echo"))
            pid = tool_pid(harness.recv_matching(has_id(1)))
            harness.send(sdk_call(2, "slow"))
            time.sleep(0.5)

            os.kill(pid, signal.SIGKILL)
            error = mcp_types.JSONRPCError.model_validate(harness.recv_matching(has_id(2), timeout=5)).error

            assert error.code == -32603
            assert "may or may not have taken effect" in error.message
        finally:
            harness.close()

    def test_a_cancelled_call_is_not_answered_at_the_death(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        harness = Harness(fake.argv)
        try:
            harness.send(fake_call(1))
            pid = harness.recv_matching(has_id(1))["result"]["pid"]
            harness.send(request(5, "slow", {}))
            harness.send(request(6, "slow", {}))
            harness.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 5}})
            time.sleep(0.3)

            os.kill(pid, signal.SIGKILL)
            harness.recv_matching(has_id(6))

            assert [frame for frame in harness.seen if frame.get("id") == 5] == []
        finally:
            harness.close()


class TestHandshakeReplay:
    def test_the_new_server_gets_the_original_initialize_then_initialized(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        harness = Harness(fake.argv)
        try:
            initialize = sdk_initialize(0)
            harness.send(initialize)
            harness.recv_matching(has_id(0))
            harness.send(sdk_initialized())
            harness.send(sdk_call(1, "echo", modern=False))
            pid = harness.recv_matching(has_id(1))["result"]["pid"]

            os.kill(pid, signal.SIGKILL)
            served, _ = harness.call_until_served(lambda rid: sdk_call(rid, "echo", modern=False))

            assert served["result"]["spawn"] == 1
            assert fake.received(1)[:2] == [json.dumps(initialize) + "\n", INITIALIZED]
            assert len([frame for frame in harness.seen if frame.get("id") == 0]) == 1
        finally:
            harness.close()

    def test_a_real_handshake_era_server_serves_after_the_replay(self, sdk_argv: list[str]) -> None:
        harness = Harness(sdk_argv)
        try:
            harness.send(sdk_initialize(0))
            harness.recv_matching(has_id(0))
            harness.send(sdk_initialized())
            harness.send(sdk_call(1, "echo", modern=False))
            first = tool_pid(harness.recv_matching(has_id(1)))

            os.kill(first, signal.SIGKILL)
            served, _ = harness.call_until_served(lambda rid: sdk_call(rid, "echo", modern=False))

            assert tool_pid(served) != first
        finally:
            harness.close()

    @pytest.mark.parametrize("refusal", [{"version": "2099-01-01"}, {"init_error": True}])
    def test_a_replay_the_new_server_refuses_ends_the_proxy(self, tmp_path: Path, refusal: dict[str, Any]) -> None:
        fake = FakeChild(tmp_path, [{}, refusal])
        harness = Harness(fake.argv)
        try:
            harness.send(sdk_initialize(0))
            harness.recv_matching(has_id(0))
            harness.send(fake_call(1))
            pid = harness.recv_matching(has_id(1))["result"]["pid"]

            os.kill(pid, signal.SIGKILL)

            assert harness.wait() == 1
        finally:
            harness.close()

    def test_the_last_successful_negotiation_is_the_one_replayed(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        harness = Harness(fake.argv)
        try:
            harness.send(sdk_initialize(0))
            harness.recv_matching(has_id(0))
            discover = sdk_discover("d1")
            harness.send(discover)
            harness.recv_matching(has_id("d1"))
            harness.send(fake_call(1))
            pid = harness.recv_matching(has_id(1))["result"]["pid"]

            os.kill(pid, signal.SIGKILL)
            harness.call_until_served(fake_call)

            assert fake.received(1)[0] == json.dumps(discover) + "\n"
            assert len([frame for frame in harness.seen if frame.get("id") == "d1"]) == 1
        finally:
            harness.close()


class TestReplayDeadline:
    def test_a_server_that_never_answers_the_replay_is_replaced(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        fake = FakeChild(tmp_path, [{}, {"delay": 30}, {}])
        harness = Harness(fake.argv, Timing(backoff=(0.05,), stable_seconds=0.0, replay_timeout=0.5))
        try:
            negotiate(harness)
            harness.send(fake_call(1))
            pid = harness.recv_matching(has_id(1))["result"]["pid"]

            os.kill(pid, signal.SIGKILL)
            served, refused = harness.call_until_served(fake_call)
            time.sleep(1.0)
            still_served, _ = harness.call_until_served(fake_call)

            assert served["result"]["spawn"] == 2
            assert still_served["result"]["spawn"] == 2
            assert refused
            assert "did not answer the session's handshake" in capfd.readouterr().err
        finally:
            harness.close()


class TestModernListen:
    def test_an_open_listen_ends_only_once_the_new_server_is_ready(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}, {"delay": 1.5}])
        harness = Harness(fake.argv)
        try:
            negotiate(harness)
            harness.send(sdk_listen("L1"))
            harness.recv_matching(is_listen_ack("L1"))
            harness.send(fake_call(1))
            pid = harness.recv_matching(has_id(1))["result"]["pid"]

            os.kill(pid, signal.SIGKILL)
            killed_at = time.monotonic()
            wait_until_down(harness)
            harness.send(sdk_listen("L2"))
            ended = harness.recv_matching(has_id("L1"))
            ended_after = time.monotonic() - killed_at
            harness.recv_matching(is_listen_ack("L2"))

            result = mcp_types.SubscriptionsListenResult.model_validate(ended["result"])
            assert result.model_dump(by_alias=True)["_meta"]["io.modelcontextprotocol/subscriptionId"] == "L1"
            assert ended_after >= 1.4
            assert [frame for frame in harness.seen if frame.get("id") == "L2"] == []
        finally:
            harness.close()

    def test_a_cancelled_listen_is_neither_completed_nor_forwarded(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}, {"delay": 1.0}])
        harness = Harness(fake.argv)
        try:
            negotiate(harness)
            harness.send(sdk_listen("L1"))
            harness.recv_matching(is_listen_ack("L1"))
            harness.send(fake_call(1))
            pid = harness.recv_matching(has_id(1))["result"]["pid"]

            os.kill(pid, signal.SIGKILL)
            wait_until_down(harness)
            harness.send(sdk_listen("L2"))
            for listen_id in ("L1", "L2"):
                harness.send(
                    {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": listen_id}}
                )
            harness.call_until_served(fake_call)

            assert [frame for frame in harness.seen if frame.get("id") in {"L1", "L2"}] == []
            assert not any('"L2"' in line for line in fake.received(1))
        finally:
            harness.close()

    def test_two_restarts_in_a_row_each_end_the_listen_and_serve(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        harness = Harness(fake.argv)
        try:
            harness.send(sdk_listen("L1"))
            harness.recv_matching(is_listen_ack("L1"))
            for generation, listen_id in enumerate(("L1", "L2")):
                served, _ = harness.call_until_served(fake_call)
                assert served["result"]["spawn"] == generation
                os.kill(served["result"]["pid"], signal.SIGKILL)
                harness.recv_matching(has_id(listen_id))
                harness.send(sdk_listen(f"L{generation + 2}"))
                harness.recv_matching(is_listen_ack(f"L{generation + 2}"))

            served, _ = harness.call_until_served(fake_call)
            completed = [frame["id"] for frame in harness.seen if frame.get("id") in {"L1", "L2", "L3"}]

            assert served["result"]["spawn"] == 2
            assert completed == ["L1", "L2"]
        finally:
            harness.close()


class TestWhileNoServerIsReady:
    def test_requests_fail_fast_ping_is_answered_and_nothing_reaches_the_unready_server(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}, {"delay": 2.0}])
        harness = Harness(fake.argv)
        try:
            negotiate(harness)
            harness.send(fake_call(1))
            pid = harness.recv_matching(has_id(1))["result"]["pid"]
            os.kill(pid, signal.SIGKILL)
            while len(fake.spawns()) < 2:
                time.sleep(0.05)

            harness.send(request(10, "tools/list", {}))
            refused = harness.recv_matching(has_id(10), timeout=5)
            harness.send(request(11, "ping"))
            pong = harness.recv_matching(has_id(11), timeout=5)
            harness.send(b'[{"jsonrpc":"2.0","id":12,"method":"ping"}]\n')
            harness.send(b"{not json\n")
            parse_error = harness.recv(timeout=5)
            harness.call_until_served(fake_call)
            null_id_errors = [frame for frame in harness.seen if frame.get("id") is None and "error" in frame]

            assert retry_reason(refused) == "server_restarting"
            assert pong["result"] == {}
            assert [frame for frame in harness.seen if frame.get("id") == 12] == []
            assert parse_error == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            assert null_id_errors == [parse_error]
            assert not any('"id": 10' in line or '"id":12' in line for line in fake.received(1))
        finally:
            harness.close()


class TestBackoff:
    def test_a_wrapper_refusing_while_the_stack_is_down_is_retried_with_backoff(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}, {"exit": 69}, {"exit": 69}, {}])
        harness = Harness(fake.argv, Timing(stable_seconds=0.0))
        try:
            negotiate(harness)
            harness.send(fake_call(1))
            pid = harness.recv_matching(has_id(1))["result"]["pid"]

            os.kill(pid, signal.SIGKILL)
            served, _ = harness.call_until_served(fake_call, timeout=30)

            starts = [spawn["at"] for spawn in fake.spawns()]
            assert served["result"]["spawn"] == 3
            assert starts[2] - starts[1] >= 1.0
            assert starts[3] - starts[2] >= 2.0
        finally:
            harness.close()

    def test_a_server_command_that_vanished_is_retried_until_it_returns(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        launcher = tmp_path / "launch-server"
        body = f"#!/bin/sh\nexec {' '.join(fake.argv)}\n"
        launcher.write_text(body, encoding="utf-8")
        launcher.chmod(0o755)
        harness = Harness([str(launcher)])
        try:
            harness.send(fake_call(1))
            pid = harness.recv_matching(has_id(1))["result"]["pid"]
            launcher.unlink()

            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
            harness.send(fake_call(2))
            refused = harness.recv_matching(has_id(2))
            launcher.write_text(body, encoding="utf-8")
            launcher.chmod(0o755)
            served, _ = harness.call_until_served(fake_call)

            assert retry_reason(refused) == "server_restarting"
            assert served["result"]["spawn"] == 1
        finally:
            harness.close()


class TestStartAndEnd:
    def test_a_first_server_that_exits_silently_ends_the_proxy_with_its_code(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{"exit": 69}])
        harness = Harness(fake.argv)
        try:
            assert harness.wait() == 69
            assert len(fake.spawns()) == 1
        finally:
            harness.close()

    def test_a_server_command_that_cannot_start_ends_the_proxy(self, tmp_path: Path) -> None:
        harness = Harness([str(tmp_path / "no-such-server")])
        try:
            assert harness.wait() == 127
        finally:
            harness.close()

    def test_client_eof_drains_every_answer_then_exits_with_the_server_code(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        harness = Harness(fake.argv)
        try:
            harness.send(fake_call(1))
            harness.send(fake_call(2))
            harness.close_input()

            answered = {harness.recv()["id"], harness.recv()["id"]}

            assert answered == {1, 2}
            assert harness.wait() == 0
        finally:
            harness.close()

    def test_client_eof_during_a_backoff_wait_exits_without_another_start(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        harness = Harness(fake.argv, Timing(backoff=(30.0,), stable_seconds=60.0))
        try:
            harness.send(fake_call(1))
            pid = harness.recv_matching(has_id(1))["result"]["pid"]
            os.kill(pid, signal.SIGKILL)
            harness.send(fake_call(2))
            harness.recv_matching(has_id(2))

            harness.close_input()

            assert harness.wait(timeout=10) == 0
            assert len(fake.spawns()) == 1
        finally:
            harness.close()

    def test_a_client_that_stops_reading_ends_the_proxy(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        proxy_in, client_write = os.pipe()
        client_read, proxy_out = os.pipe()
        os.close(client_read)
        with os.fdopen(proxy_out, "wb", buffering=0) as out, os.fdopen(client_write, "wb") as to_proxy:
            proxy = StdioProxy(fake.argv, client_in=proxy_in, client_out=out)
            to_proxy.write(json.dumps(fake_call(1)).encode() + b"\n")
            to_proxy.flush()

            assert proxy.run() == 0
        os.close(proxy_in)


class TestTransparency:
    def test_a_negotiation_result_is_byte_identical_to_the_direct_server(self, sdk_argv: list[str]) -> None:
        payload = json.dumps(sdk_discover("d1")).encode() + b"\n"
        direct = subprocess.run(sdk_argv, input=payload, capture_output=True, timeout=30, check=False).stdout

        harness = Harness(sdk_argv)
        try:
            harness.send(payload)
            proxied = harness.recv_raw()
        finally:
            harness.close()

        assert proxied == direct

    def test_an_eight_mebibyte_frame_passes_unchanged_both_ways(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        size = 8 << 20
        upstream = json.dumps(request(1, "digest", {"blob": "x" * size})).encode() + b"\n"
        downstream = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"blob": "y" * size}}).encode() + b"\n"
        harness = Harness(fake.argv)
        try:
            harness.send(upstream)
            digest = harness.recv(timeout=30)["result"]["sha256"]
            harness.send(request(2, "big", {"size": size}))
            received = harness.recv_raw(timeout=30)
        finally:
            harness.close()

        assert digest == hashlib.sha256(upstream).hexdigest()
        assert received == downstream

    def test_a_malformed_frame_while_ready_reaches_the_server_and_its_reply_comes_back_verbatim(
        self, tmp_path: Path
    ) -> None:
        fake = FakeChild(tmp_path, [{}])
        harness = Harness(fake.argv)
        try:
            harness.send(b"{not json\n")

            assert harness.recv_raw() == b"{not json\n"
        finally:
            harness.close()

    def test_a_server_flooding_stderr_still_answers(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{"stderr": 1 << 20}])
        harness = Harness(fake.argv)
        try:
            harness.send(fake_call(1))

            assert harness.recv_matching(has_id(1), timeout=10)["result"]["spawn"] == 0
        finally:
            harness.close()

    def test_a_request_written_after_the_server_closed_its_input_gets_the_restarted_error(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{"close_after_reply": True, "linger": 1.0}, {}])
        harness = Harness(fake.argv)
        try:
            harness.send(fake_call(1))
            harness.recv_matching(has_id(1))
            time.sleep(0.3)

            harness.send(fake_call(2))

            assert retry_reason(harness.recv_matching(has_id(2))) == "server_restarted"
        finally:
            harness.close()


class TestHostDirectory:
    def test_a_respawn_after_the_launch_dir_was_deleted_runs_from_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeChild(tmp_path, [{}])
        launch = tmp_path / "session-cwd"
        launch.mkdir()
        monkeypatch.chdir(launch)
        harness = Harness(fake.argv)
        try:
            harness.send(fake_call(1))
            pid = harness.recv_matching(has_id(1))["result"]["pid"]
            monkeypatch.chdir(tmp_path)
            launch.rmdir()

            os.kill(pid, signal.SIGKILL)
            harness.call_until_served(fake_call)

            assert [spawn["cwd"] for spawn in fake.spawns()] == [str(launch), "/"]
        finally:
            harness.close()

    def test_a_proxy_launched_from_a_deleted_dir_starts_its_server_from_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeChild(tmp_path, [{}])
        launch = tmp_path / "gone"
        launch.mkdir()
        monkeypatch.chdir(launch)
        launch.rmdir()
        harness = Harness(fake.argv)
        monkeypatch.chdir(tmp_path)
        try:
            harness.send(fake_call(1))
            harness.recv_matching(has_id(1))

            assert fake.spawns()[0]["cwd"] == "/"
        finally:
            harness.close()


class TestShutdown:
    def test_a_server_outliving_client_eof_is_terminated_after_the_grace(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{"linger_after_eof": 30}])
        harness = Harness(fake.argv, Timing(eof_grace=0.2, kill_grace=5.0))
        try:
            harness.send(fake_call(1))
            harness.recv_matching(has_id(1))

            harness.close_input()

            assert harness.wait(timeout=10) == 128 + signal.SIGTERM
        finally:
            harness.close()

    def test_a_server_ignoring_sigterm_after_eof_is_killed(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{"linger_after_eof": 30, "ignore_term": True}])
        harness = Harness(fake.argv, Timing(eof_grace=0.2, kill_grace=0.2))
        try:
            harness.send(fake_call(1))
            harness.recv_matching(has_id(1))

            harness.close_input()

            assert harness.wait(timeout=10) == 128 + signal.SIGKILL
        finally:
            harness.close()

    def test_a_stop_reaches_the_child_at_once_and_a_grandchild_that_ignores_it(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        fake = FakeChild(tmp_path, [{"grandchild": True}])
        harness = Harness(fake.argv)
        try:
            harness.send(fake_call(1))
            harness.recv_matching(has_id(1))

            harness.proxy.request_stop(signal.SIGINT)

            assert harness.wait() == 0
            assert all(is_gone_within(pid, seconds=5) for pid in fake.family())
            assert f"exited with code {128 + signal.SIGINT}" in capfd.readouterr().err
        finally:
            harness.close()

    def test_a_repeated_stop_kills_at_once_instead_of_waiting_out_the_grace(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{"grandchild": True, "ignore_term": True}])
        harness = Harness(fake.argv, Timing(signal_grace=30.0))
        try:
            harness.send(fake_call(1))
            harness.recv_matching(has_id(1))

            harness.proxy.request_stop(signal.SIGINT)
            harness.proxy.request_stop(signal.SIGTERM)

            assert harness.wait(timeout=10) == 0
            assert all(is_gone_within(pid, seconds=5) for pid in fake.family())
        finally:
            harness.close()

    def test_client_eof_during_a_stop_keeps_the_stop_deadline(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{"grandchild": True}])
        harness = Harness(fake.argv, Timing(signal_grace=0.5, eof_grace=30.0))
        try:
            harness.send(fake_call(1))
            harness.recv_matching(has_id(1))

            harness.proxy.request_stop(signal.SIGTERM)
            harness.close_input()

            assert harness.wait(timeout=10) == 0
        finally:
            harness.close()

    def test_a_stop_while_no_server_runs_exits_at_once(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        harness = Harness(fake.argv, Timing(backoff=(30.0,), stable_seconds=60.0))
        try:
            harness.send(fake_call(1))
            pid = harness.recv_matching(has_id(1))["result"]["pid"]
            os.kill(pid, signal.SIGKILL)
            wait_until_down(harness)

            harness.proxy.request_stop(signal.SIGTERM)

            assert harness.wait(timeout=10) == 0
        finally:
            harness.close()

    def test_a_server_death_sweeps_what_is_left_of_its_process_group(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{"grandchild": True, "grandchild_closes_stdout": True}])
        harness = Harness(fake.argv)
        try:
            harness.send(fake_call(1))
            harness.recv_matching(has_id(1))

            harness.close_input()

            assert harness.wait() == 0
            assert all(is_gone_within(pid, seconds=5) for pid in fake.family())
        finally:
            harness.close()

    def test_a_server_whose_output_never_closes_is_abandoned_after_the_kill(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{"escape": True}])
        harness = Harness(fake.argv, Timing(eof_grace=0.1, kill_grace=0.2))
        try:
            harness.send(fake_call(1))
            harness.recv_matching(has_id(1))

            harness.close_input()

            assert harness.wait(timeout=10) == 0
        finally:
            for pid in fake.family():
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
            harness.close()
