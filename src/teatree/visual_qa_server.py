"""Start and stop a checkout server in the browser runner's network namespace."""

import contextlib
import os
import signal
import socket
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from teatree.types import RunCommand
from teatree.utils.run import Popen, TimeoutExpired, redact_secrets, spawn_session_leader

SERVER_START_TIMEOUT_S = 90


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
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pgid, signal.SIGTERM)
        try:
            self.process.wait(timeout=5)
        except TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.pgid, signal.SIGKILL)
            self.process.wait(timeout=5)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pgid, signal.SIGKILL)


@dataclass(frozen=True, slots=True)
class ServerSpec:
    command: RunCommand
    repo_path: str
    prepare: Callable[[], None]


@contextlib.contextmanager
def served_app(server: ServerSpec) -> Iterator[ServedApp]:
    with tempfile.TemporaryDirectory(prefix="teatree-visual-qa-") as scratch:
        server.prepare()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        env = dict(os.environ)
        for key in ("T3_CONFIG_DB", "T3_CONTROL_DB_DIR", "T3_DATA_DIR", "DATABASE_URL", "DJANGO_SETTINGS_MODULE"):
            env.pop(key, None)
        env.update(server.command.env)
        env.update({"XDG_DATA_HOME": scratch, "TMPDIR": scratch, "T3_VISUAL_QA_PORT": str(port)})
        log_path = Path(scratch) / "server.log"
        with log_path.open("w+") as log:
            try:
                process = spawn_session_leader(
                    server.command.args,
                    cwd=server.command.cwd or server.repo_path,
                    env=env,
                    stdout=log,
                    stderr=log,
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
