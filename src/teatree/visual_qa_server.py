"""Start and stop a checkout server in the browser runner's network namespace."""

import contextlib
import os
import signal
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path

from teatree.types import RunCommand
from teatree.utils.run import PIPE, Popen, TimeoutExpired, redact_secrets, spawn, spawn_session_leader

SERVER_START_TIMEOUT_S = 90
STOP_GRACE_S = 5
SCRATCH_PREFIX = "teatree-visual-qa-"
DATABASE_OVERRIDES = ("T3_CONFIG_DB", "T3_CONTROL_DB_DIR", "DATABASE_URL")


class VisualQAUnavailableError(RuntimeError):
    """The gate did not run, so its result cannot pass."""


@dataclass(slots=True)
class ServedApp:
    base_url: str
    process: Popen[str]
    pgid: int
    log_path: Path

    def failure(self) -> str:
        code = self.process.poll()
        if code is None:
            return ""
        tail = redact_secrets("\n".join(self.log_path.read_text(errors="replace").splitlines()[-12:]))
        return f"server exited {code}: {tail}" if tail else f"server exited {code}"

    def wait_until_ready(self, port: int) -> None:
        deadline = time.monotonic() + SERVER_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if failure := self.failure():
                raise VisualQAUnavailableError(failure)
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.2)
        message = f"checkout server did not listen within {SERVER_START_TIMEOUT_S}s"
        raise VisualQAUnavailableError(message)

    def stop(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=STOP_GRACE_S)
        except TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.pgid, signal.SIGKILL)
            self.process.wait(timeout=STOP_GRACE_S)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pgid, signal.SIGKILL)


@dataclass(frozen=True, slots=True)
class ServerSpec:
    command: RunCommand
    repo_path: str
    prepare: Callable[[], None]


@contextlib.contextmanager
def served_app(server: ServerSpec) -> Generator[ServedApp]:
    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as scratch:
        server.prepare()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        env = dict(os.environ)
        for key in (*DATABASE_OVERRIDES, "T3_DATA_DIR", "DJANGO_SETTINGS_MODULE"):
            env.pop(key, None)
        env.update(server.command.env)
        env.update({"XDG_DATA_HOME": scratch, "TMPDIR": scratch, "T3_VISUAL_QA_PORT": str(port)})
        log_path = Path(scratch) / "server.log"
        with log_path.open("w+") as log:
            try:
                process = spawn_session_leader(
                    [sys.executable, "-m", "teatree.visual_qa_server", *server.command.args],
                    cwd=server.command.cwd or server.repo_path,
                    env=env,
                    stdio=(PIPE, log, log),
                )
            except OSError as exc:
                message = f"checkout server could not start: {exc}"
                raise VisualQAUnavailableError(message) from exc
            served = ServedApp(f"http://127.0.0.1:{port}", process, process.pid, log_path)
            try:
                served.wait_until_ready(port)
                yield served
            finally:
                served.stop()


def _watch_parent_stdin(fd: int) -> None:
    with os.fdopen(fd, "rb") as stream:
        stream.read(1)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgrp(), signal.SIGTERM)


def _serve_until_exit(command: list[str]) -> int:
    parent_input = os.dup(0)
    with Path(os.devnull).open("rb") as devnull:
        os.dup2(devnull.fileno(), 0)
    child = spawn(command)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: None)
    threading.Thread(target=_watch_parent_stdin, args=(parent_input,), daemon=True).start()
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(_serve_until_exit(sys.argv[1:]))
