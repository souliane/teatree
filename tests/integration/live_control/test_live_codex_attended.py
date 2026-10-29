"""Opt-in Codex session proof against the interactive mailbox server."""

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from django.test import TestCase

from tests.teatree_cli.test_cli_mcp import _attended_driver, _attended_worker


def _prompt(address: str, nonce: str) -> str:
    return (
        f"Use agent_mailbox_peers to find worker-agent at {address}. "
        f"Send exactly {nonce} to that peer with a fresh key. "
        "Then use agent_mailbox_wait until the peer replies. Include the reply nonce in your final answer."
    )


def _run_codex(
    executable: str, directory: Path, code_home: Path, env: dict[str, str], prompt: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            executable,
            "exec",
            "--ephemeral",
            "--approve-for-me",
            "--skip-git-repo-check",
            "--output-last-message",
            str(directory / "answer.txt"),
            "-C",
            str(directory),
            prompt,
        ],
        env={**env, "CODEX_HOME": str(code_home)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@pytest.mark.integration
@pytest.mark.timeout(360)
class LiveAttendedCodexTests(TestCase):
    def test_real_codex_exchanges_nonces_with_a_worker_peer(self) -> None:
        if os.environ.get("T3_LIVE_INTERACTIVE") != "1":
            self.skipTest("set T3_LIVE_INTERACTIVE=1 for the attended Codex proof")
        executable = shutil.which("codex")
        auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
        if executable is None or not auth.is_file():
            self.skipTest("Codex executable or login is unavailable for the attended proof")
        nonce_to_peer, nonce_to_attended = secrets.token_hex(12), secrets.token_hex(12)
        with _attended_worker() as (directory, env, registered, peer):
            code_home = directory / "codex"
            code_home.mkdir(mode=0o700)
            shutil.copy2(auth, code_home / "auth.json")
            (code_home / "config.toml").write_text(
                "[mcp_servers.teatree]\n"
                f"command = {json.dumps(sys.executable)}\n"
                f"args = {json.dumps(['-c', _attended_driver(interactive=True)])}\n"
                "[mcp_servers.teatree.env]\n"
                + "\n".join(
                    f"{key} = {json.dumps(env[key])}"
                    for key in (
                        "T3_CONTROL_DB_DIR",
                        "T3_MCP_DELEGATED",
                        "DJANGO_SETTINGS_MODULE",
                        "PYTHONPATH",
                        "XDG_DATA_HOME",
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            async def attend() -> subprocess.CompletedProcess[str]:
                return await asyncio.to_thread(
                    _run_codex,
                    executable,
                    directory,
                    code_home,
                    env,
                    _prompt(registered["address"], nonce_to_peer),
                )

            async def converse(attended: asyncio.Task[subprocess.CompletedProcess[str]]) -> tuple[str, str] | None:
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    page = await peer.wait(timeout_seconds=2)
                    if page["messages"]:
                        received = page["messages"][0]
                        reply = await peer.send(received["from"], nonce_to_attended, "codex-attended-reply")
                        return received["text"], reply["outcome"]
                    if attended.done():
                        break
                return None

            async def exchange() -> tuple[subprocess.CompletedProcess[str], tuple[str, str] | None]:
                attended = asyncio.create_task(attend())
                reply = await converse(attended)
                return await attended, reply

            result, reply = asyncio.run(exchange())
            assert result.returncode == 0, f"Codex exited {result.returncode}"
            answer = (directory / "answer.txt").read_text(encoding="utf-8")
        assert reply is not None, "Codex sent no mailbox message"
        received_text, reply_outcome = reply
        assert received_text == nonce_to_peer
        assert reply_outcome == "accepted_inbox_read"
        assert nonce_to_attended in answer
        sys.stdout.write(
            json.dumps(
                {
                    "node": "codex_attended",
                    "reply_outcome": reply_outcome,
                    "nonce_to_peer_received": received_text == nonce_to_peer,
                    "nonce_to_attended_answered": nonce_to_attended in answer,
                },
                sort_keys=True,
            )
            + "\n"
        )
