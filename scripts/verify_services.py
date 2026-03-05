#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "typer>=0.12",
# ]
# ///
"""Verify dev services are running via HTTP health checks.

Checks backend, frontend, and API endpoints. Uses ports from .env.worktree
or explicit arguments.

Used by: t3-workspace (after start_session), t3-debug (diagnose startup failures).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import typer

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from lib.env import detect_ticket_dir, read_env_key

# Default endpoint paths — project overlays can override via T3_HEALTH_ENDPOINTS env
_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 400

_DEFAULT_ENDPOINTS = {
    "backend": {"port_env": "BACKEND_PORT", "path": "/admin/login/", "default_port": 8000},
    "frontend": {"port_env": "FRONTEND_PORT", "path": "/", "default_port": 4200},
}


def _check_endpoint(host: str, port: int, path: str, *, timeout: int = 5) -> dict:
    """HTTP GET and return {status_code, ok, error}."""
    url = f"http://{host}:{port}{path}"
    result = subprocess.run(
        ["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(timeout), url],
        capture_output=True,
        text=True,
        check=False,
    )
    status_code = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
    return {
        "url": url,
        "status_code": status_code,
        "ok": status_code >= _HTTP_OK_MIN and status_code < _HTTP_OK_MAX,
        "error": result.stderr.strip() if status_code == 0 else None,
    }


def _load_custom_endpoints() -> dict | None:
    """Load custom endpoints from T3_HEALTH_ENDPOINTS env (JSON)."""
    raw = os.environ.get("T3_HEALTH_ENDPOINTS", "")
    if raw:
        return json.loads(raw)
    return None


def verify(
    *,
    backend_port: int = 0,
    frontend_port: int = 0,
    host: str = "localhost",
    timeout: int = 5,
) -> dict:
    """Verify all services. Returns {service_name: {url, status_code, ok, error}}."""
    # Resolve ports from env or .env.worktree
    td = detect_ticket_dir()
    envwt = str(Path(td) / ".env.worktree") if td else ""

    endpoints = _load_custom_endpoints() or _DEFAULT_ENDPOINTS
    results: dict = {}

    for name, config in endpoints.items():
        port_env = str(config.get("port_env", ""))
        default_port = int(config.get("default_port", 8000))
        ep_path = str(config.get("path", "/"))

        # Port resolution: explicit > env var > .env.worktree > default
        port = 0
        if name == "backend" and backend_port:
            port = backend_port
        elif name == "frontend" and frontend_port:
            port = frontend_port

        if not port and port_env:
            port = int(os.environ.get(port_env) or "0")
        if not port and envwt and port_env:
            port_str = read_env_key(envwt, port_env)
            port = int(port_str) if port_str else 0
        if not port:
            port = default_port

        results[name] = _check_endpoint(host, port, ep_path, timeout=timeout)

    return results


def main(
    backend_port: int = typer.Option(0, "--backend-port", "-b", help="Backend port override"),
    frontend_port: int = typer.Option(0, "--frontend-port", "-f", help="Frontend port override"),
    timeout: int = typer.Option(5, "--timeout", help="HTTP timeout in seconds"),
) -> None:
    """Verify dev services are running."""
    results = verify(backend_port=backend_port, frontend_port=frontend_port, timeout=timeout)

    all_ok = True
    for name, check in results.items():
        status = "OK" if check["ok"] else "FAIL"
        code = check["status_code"]
        url = check["url"]
        error = f" ({check['error']})" if check.get("error") else ""
        print(f"  {name}: {status} [{code}] {url}{error}")
        if not check["ok"]:
            all_ok = False

    if all_ok:
        print("All services running")
    else:
        print("Some services failed health check", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    typer.run(main)
