# test-path: cross-cutting — drives deploy/t3 through its Docker boundary.
"""A worker replacement must not close an interactive MCP client's stdio."""

import json
import os
import select
import shutil
import stat
import subprocess
from pathlib import Path

from _deploy_wrapper_paths import copy_wrapper

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
_BASH = shutil.which("bash") or "/bin/bash"


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path, Path]:
    deploy = tmp_path / "checkout" / "deploy"
    deploy.mkdir(parents=True)
    wrapper = deploy / "t3"
    copy_wrapper(WRAPPER, wrapper)
    docker = tmp_path / "docker"
    calls = tmp_path / "calls"
    worker = tmp_path / "worker-generation"
    worker.write_text("1", encoding="utf-8")
    server = tmp_path / "server.py"
    server.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "generation = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else None\n"
        "for line in sys.stdin:\n"
        "    if generation is not None and Path(sys.argv[1]).read_text() != generation:\n"
        "        break\n"
        "    print(json.dumps({'id': json.loads(line)['id']}), flush=True)\n",
        encoding="utf-8",
    )
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$MCP_DOCKER_CALLS"\n'
        'case "$1" in\n'
        '  ps) printf "worker-id teatree-worker False\\n"; exit 0 ;;\n'
        '  inspect) printf "teatree-headless:latest bad-revision\\n"; exit 0 ;;\n'
        "esac\n"
        'case " $* " in\n'
        '  *" exec "*) exec python3 "$MCP_FAKE_SERVER" "$MCP_WORKER_GENERATION" ;;\n'
        '  *" run "*) exec python3 "$MCP_FAKE_SERVER" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("T3_", "TEATREE_"))}
    env.update(
        PATH=f"{tmp_path}:{env['PATH']}",
        HOME=str(tmp_path),
        MCP_DOCKER_CALLS=str(calls),
        MCP_FAKE_SERVER=str(server),
        MCP_WORKER_GENERATION=str(worker),
    )
    return wrapper, env, calls, worker


def _request(server: subprocess.Popen[bytes], request_id: int) -> dict[str, int]:
    assert server.stdin is not None
    assert server.stdout is not None
    server.stdin.write(json.dumps({"id": request_id}).encode() + b"\n")
    server.stdin.flush()
    readable, _, _ = select.select([server.stdout], [], [], 5)
    assert readable, "MCP response timed out"
    response = server.stdout.readline()
    assert response, "MCP stdio closed after worker restart"
    return json.loads(response)


def test_mcp_connection_survives_worker_replacement(tmp_path: Path) -> None:
    wrapper, env, calls, worker = _fixture(tmp_path)
    with subprocess.Popen(
        [_BASH, str(wrapper), "mcp", "serve", "--read-only"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as server:
        assert _request(server, 1) == {"id": 1}
        worker.write_text("2", encoding="utf-8")
        assert _request(server, 2) == {"id": 2}

    dispatch = calls.read_text(encoding="utf-8")
    assert " run --rm --no-deps -T " in dispatch
    assert " mcp serve --read-only" in dispatch


def test_other_commands_still_exec_in_running_worker(tmp_path: Path) -> None:
    wrapper, env, calls, _worker = _fixture(tmp_path)
    result = subprocess.run(
        [_BASH, str(wrapper), "doctor", "check"],
        cwd=tmp_path,
        env=env,
        input=b"",
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert " exec -T " in calls.read_text(encoding="utf-8")
