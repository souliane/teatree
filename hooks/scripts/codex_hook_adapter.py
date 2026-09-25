import argparse
import json
import subprocess  # noqa: S404 — the adapter must execute TeaTree's repository-owned hook router
import sys
from pathlib import Path

_ROUTER = Path(__file__).with_name("hook_router.py")
_SPAWN_ERROR = "TeaTree Codex hook adapter could not start the shared hook router.\n"
_CLAUDE_BLOCK_EXIT = 2


def _codex_accepts_stdout_deny(event: str, returncode: int, stdout: str) -> bool:
    if event != "PreToolUse" or returncode != _CLAUDE_BLOCK_EXIT:
        return False
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    specific = payload.get("hookSpecificOutput") if isinstance(payload, dict) else None
    return isinstance(specific, dict) and specific.get("permissionDecision") == "deny"


def run_codex_hook(event: str, payload: str) -> tuple[int, str, str]:
    try:
        result = subprocess.run(  # noqa: S603 — argv contains only the interpreter and repository-owned router path
            [sys.executable, str(_ROUTER), "--event", event],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return 1, "", _SPAWN_ERROR
    if _codex_accepts_stdout_deny(event, result.returncode, result.stdout):
        return 0, result.stdout, result.stderr
    return result.returncode, result.stdout, result.stderr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    args = parser.parse_args()
    returncode, stdout, stderr = run_codex_hook(args.event, sys.stdin.read())
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
