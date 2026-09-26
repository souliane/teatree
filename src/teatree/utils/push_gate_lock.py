import fcntl
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

LOCK_FD = 9
HEARTBEAT_SECONDS = 30.0
ABORTED_EXIT_CODE = 75


def _holder_record() -> str:
    pid = os.environ.get("T3_PUSH_GATE_HOLDER_PID", str(os.getppid()))
    acquired = datetime.now(UTC).isoformat(timespec="seconds")
    return f"pid={pid} repo={Path.cwd()} acquired={acquired}"


def _read_holder(fd: int) -> str:
    try:
        holder = os.pread(fd, 4096, 0).decode(errors="replace").strip()
    except OSError:
        return "unknown"
    return holder or "unknown"


def _write_holder(fd: int) -> None:
    record = f"{_holder_record()}\n".encode()
    os.ftruncate(fd, 0)
    os.pwrite(fd, record, 0)
    os.fsync(fd)


def _emit(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


def acquire_push_gate_lock(max_wait_seconds: float, *, fd: int = LOCK_FD) -> int:
    started = time.monotonic()
    deadline = started + max(0.0, max_wait_seconds)
    next_heartbeat = started

    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            now = time.monotonic()
            elapsed = max(0, int(now - started))
            maximum = max(0, int(max_wait_seconds))
            if now >= next_heartbeat:
                _emit(
                    f"=== push-gate: waiting for lock held by {_read_holder(fd)}; elapsed={elapsed}s max={maximum}s ==="
                )
                next_heartbeat = now + HEARTBEAT_SECONDS
            if now >= deadline:
                _emit(
                    f"=== push-gate: ABORTED waiting for lock held by {_read_holder(fd)}; "
                    f"elapsed={elapsed}s max={maximum}s ==="
                )
                return ABORTED_EXIT_CODE
            time.sleep(min(1.0, deadline - now))
        else:
            _write_holder(fd)
            return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 2
    try:
        max_wait_seconds = float(arguments[0])
    except ValueError:
        return 2
    return acquire_push_gate_lock(max_wait_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
