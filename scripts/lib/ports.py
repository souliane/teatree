"""Port management helpers."""

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress


def port_in_use(port: int) -> bool:
    """Check if a TCP port is already listening."""
    result = subprocess.run(
        ["lsof", "-i", f":{port}", "-sTCP:LISTEN"],
        capture_output=True,
    )
    return result.returncode == 0


def free_port(port: int) -> bool:
    """Kill process listening on port. Returns True if port is free after attempt."""
    if not port_in_use(port):
        return True

    print(f"  Stopping process on port {port}...")
    result = subprocess.run(
        ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        for pid in result.stdout.strip().split("\n"):
            with suppress(OSError, ValueError):
                os.kill(int(pid.strip()), signal.SIGTERM)

    time.sleep(1)
    if port_in_use(port):
        print(
            f"ERROR: Port {port} still in use. Stop the process manually.",
            file=sys.stderr,
        )
        return False
    return True
