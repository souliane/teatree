"""Frames, classification, backoff and host compatibility of the MCP stdio proxy."""

import ast
import json
import os
import signal
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from mcp import types as mcp_types

from teatree.utils import mcp_stdio_proxy as proxy_module
from teatree.utils.mcp_stdio_proxy import (
    BACKOFF_SECONDS,
    BATCH,
    MALFORMED,
    NOTIFICATION,
    REQUEST,
    RESPONSE,
    backoff_delay,
    classify,
    iter_lines,
    listen_complete,
    main,
    restarted_error,
    restarting_error,
    watch_parent,
)
from tests.teatree_utils._mcp_proxy_children import FakeChild, Harness, request

if TYPE_CHECKING:
    from collections.abc import Callable

SOURCE = Path(proxy_module.__file__)


class TestFramesValidateAgainstTheSdk:
    @pytest.mark.parametrize("listen_id", ["listen-7", 7])
    def test_the_listen_completion_is_a_subscriptions_listen_result(self, listen_id: object) -> None:
        frame = json.loads(listen_complete(listen_id))

        result = mcp_types.SubscriptionsListenResult.model_validate(frame["result"])

        assert frame["id"] == listen_id
        assert result.model_dump(by_alias=True)["_meta"]["io.modelcontextprotocol/subscriptionId"] == listen_id
        assert result.result_type == "complete"

    @pytest.mark.parametrize(
        ("frame", "reason"),
        [
            (restarting_error(3, 4.2, 137), "server_restarting"),
            (restarted_error("call-9"), "server_restarted"),
        ],
    )
    def test_the_errors_are_retryable_internal_errors(self, frame: bytes, reason: str) -> None:
        error = mcp_types.JSONRPCError.model_validate(json.loads(frame)).error

        assert error.code == -32603
        assert error.data == {"retryable": True, "reason": reason}

    def test_the_in_flight_error_says_the_call_may_have_run(self) -> None:
        message = json.loads(restarted_error(1))["error"]["message"]

        assert "may or may not have taken effect" in message

    def test_the_restarting_error_names_how_long_and_how_it_exited(self) -> None:
        message = json.loads(restarting_error(1, 4.2, 137))["error"]["message"]

        assert "4s ago" in message
        assert "code 137" in message

    def test_every_frame_is_one_terminated_line(self) -> None:
        for frame in (listen_complete("a"), restarted_error(1), restarting_error(1, 0.0, 0)):
            assert frame.endswith(b"\n")
            assert frame.count(b"\n") == 1


class TestClassify:
    @pytest.mark.parametrize(
        ("line", "kind", "method"),
        [
            (b'{"jsonrpc":"2.0","id":1,"method":"tools/call"}\n', REQUEST, "tools/call"),
            (
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
                NOTIFICATION,
                "notifications/initialized",
            ),
            (b'{"jsonrpc":"2.0","id":1,"result":{}}\n', RESPONSE, None),
            (b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]\n', BATCH, None),
            (b"{not json\n", MALFORMED, None),
            (b'"a string"\n', MALFORMED, None),
            (b'{"jsonrpc":"2.0","id":{"nested":1},"method":"ping"}\n', MALFORMED, None),
            (b'{"jsonrpc":"2.0"}\n', MALFORMED, None),
            (b"\xff\xfe\n", MALFORMED, None),
        ],
    )
    def test_sorts_each_shape(self, line: bytes, kind: str, method: str | None) -> None:
        message = classify(line)

        assert (message.kind, message.method) == (kind, method)

    def test_an_int_and_a_string_id_are_distinct_keys(self) -> None:
        as_int = classify(b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
        as_str = classify(b'{"jsonrpc":"2.0","id":"1","method":"ping"}')

        assert as_int.key != as_str.key

    def test_a_cancellation_is_keyed_by_the_request_it_cancels(self) -> None:
        cancel = classify(b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":"L1"}}')

        assert cancel.key == classify(b'{"jsonrpc":"2.0","id":"L1","method":"subscriptions/listen"}').key


class TestBackoff:
    def test_the_schedule_doubles_then_caps(self) -> None:
        assert [backoff_delay(failures) for failures in range(8)] == [0, 1, 2, 4, 8, 16, 30, 30]

    def test_a_long_outage_is_noticed_within_one_cap(self) -> None:
        attempts, elapsed = [], 0.0
        for failures in range(1, 12):
            elapsed += backoff_delay(failures)
            attempts.append(elapsed)

        first_after_outage = next(at for at in attempts if at >= 60.0)
        assert first_after_outage - 60.0 <= BACKOFF_SECONDS[-1]


class TestIterLines:
    def test_splits_on_newlines_and_keeps_a_trailing_partial_line(self) -> None:
        read_fd, write_fd = os.pipe()
        with os.fdopen(write_fd, "wb") as writer:
            writer.write(b'{"a":1}\n{"b":2}\npartial')

        lines = list(iter_lines(read_fd))
        os.close(read_fd)

        assert lines == [b'{"a":1}\n', b'{"b":2}\n', b"partial"]

    def test_a_line_far_larger_than_one_read_is_returned_whole(self) -> None:
        read_fd, write_fd = os.pipe()
        payload = b"z" * (3 * 65536 + 17) + b"\n"
        writer = threading.Thread(target=lambda: os.write(write_fd, payload) and os.close(write_fd))
        writer.start()

        lines = list(iter_lines(read_fd))
        writer.join()
        os.close(read_fd)

        assert lines == [payload]


def evaluated_annotations(tree: ast.Module) -> list[ast.expr]:
    """Signature, class-body and module-level annotations: the ones Python before 3.14 evaluates."""
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            found += [arg.annotation for arg in arguments if arg.annotation is not None]
            found += [node.returns] if node.returns is not None else []
        elif isinstance(node, (ast.Module, ast.ClassDef)):
            found += [stmt.annotation for stmt in node.body if isinstance(stmt, ast.AnnAssign)]
    return found


class TestHostCompatibility:
    """deploy/t3 runs this file under the host python3, which is 3.9 on macOS."""

    def test_parses_as_python_3_9(self) -> None:
        ast.parse(SOURCE.read_text(encoding="utf-8"), feature_version=(3, 9))

    def test_no_evaluated_annotation_uses_a_union(self) -> None:
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))

        unions = [
            ast.unparse(annotation)
            for annotation in evaluated_annotations(tree)
            if any(isinstance(part, ast.BinOp) and isinstance(part.op, ast.BitOr) for part in ast.walk(annotation))
        ]
        assert unions == []

    def test_the_union_guard_sees_signatures_and_class_bodies(self) -> None:
        tree = ast.parse("def f(a: int | None) -> str | None: ...\nclass C:\n    x: int | None = None\n")

        assert len(evaluated_annotations(tree)) == 3

    def test_imports_only_the_standard_library(self) -> None:
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        }
        imported |= {
            node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
        }

        assert imported
        assert imported <= set(sys.stdlib_module_names)


class TestMainSeams:
    def test_without_a_server_command_it_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([]) == 2
        assert "usage" in capsys.readouterr().err

    def test_main_installs_the_stop_handlers_and_a_signal_stops_the_server(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        installed: dict[int, Callable[[int, object], None]] = {}
        proxy_in, client_write = os.pipe()
        client_read, proxy_out = os.pipe()
        results: list[int] = []
        with os.fdopen(proxy_out, "wb") as out, os.fdopen(client_write, "wb") as to_proxy:
            runner = threading.Thread(
                target=lambda: results.append(
                    main(fake.argv, client_in=proxy_in, client_out=out, install=installed.__setitem__)
                )
            )
            runner.start()
            to_proxy.write(json.dumps(request(1, "tools/call", {})).encode() + b"\n")
            to_proxy.flush()
            with os.fdopen(client_read, "rb") as from_proxy:
                served = json.loads(from_proxy.readline())
                installed[signal.SIGTERM](signal.SIGTERM, None)
                runner.join(10)
        os.close(proxy_in)

        assert set(installed) == {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
        assert served["result"]["spawn"] == 0
        assert results == [0]

    def test_the_parent_watch_stops_the_proxy_once_the_parent_changes(self) -> None:
        parents = iter([4242, 4242, 4242, 1])
        stops: list[int] = []

        watch_parent(stops.append, getppid=lambda: next(parents), interval=0.01).join(5)

        assert stops == [signal.SIGTERM]

    def test_a_proxy_whose_parent_died_stops_its_server(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        harness = Harness(fake.argv)
        try:
            harness.send(request(1, "tools/call", {}))
            harness.recv()
            parents = iter([4242, 1])

            watch_parent(harness.proxy.request_stop, getppid=lambda: next(parents), interval=0.01)

            assert harness.wait() == 0
        finally:
            harness.close()
