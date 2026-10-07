"""A stream-json stand-in for the ``claude`` CLI that replays the recorded 2.1.284 live-control contract.

The real SDK spawns it (``ClaudeAgentOptions.cli_path``), so the SDK's control channel, the hook
callbacks and teatree's harness run unmodified. ``T3_FAKE_CLAUDE_SCRIPT`` (JSON) scripts one turn;
every line the SDK writes and every hook answer it gives is appended to ``T3_FAKE_CLAUDE_LOG``.

Script keys: ``steps`` — ``{"tool": name, "hold_until": path, "subagent": bool}`` or
``{"text": str}``; ``final_text``; ``hold_before_stop`` / ``hold_after_stop`` — paths the turn
waits on just before its first Stop boundary / after its final one, before the result.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "claude_cli" / "2.1.284-live-control.json"
CONTRACT = json.loads(FIXTURE.read_text(encoding="utf-8"))
_HOLD_LIMIT_SECONDS = 60
_MAX_STOP_CONTINUATIONS = 5


class FakeClaudeCli:
    def __init__(self, script: dict[str, Any], log: Path) -> None:
        self.script = script
        self.log = log
        self.session_id = str(uuid.uuid4())
        self.hooks: dict[str, list[str]] = {}
        self.turns = 0
        self.tool_ids = 0

    def record(self, event: str, **data: object) -> None:
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, **data}) + "\n")

    def emit(self, message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps({**message, "session_id": self.session_id}) + "\n")
        sys.stdout.flush()

    def read(self) -> dict[str, Any] | None:
        line = sys.stdin.readline()
        if not line:
            return None
        message = json.loads(line)
        self.record("stdin", message=message)
        if message.get("type") == "control_request":
            self.answer_control(message)
        return message

    def answer_control(self, message: dict[str, Any]) -> None:
        request = message["request"]
        if request.get("subtype") == "initialize":
            for event, matchers in (request.get("hooks") or {}).items():
                self.hooks[event] = [cid for matcher in matchers for cid in matcher["hookCallbackIds"]]
            response: dict[str, Any] = {"commands": [], "output_style": "default"}
        else:
            response = {}
        sys.stdout.write(
            json.dumps(
                {
                    "type": "control_response",
                    "response": {"subtype": "success", "request_id": message["request_id"], "response": response},
                }
            )
            + "\n"
        )
        sys.stdout.flush()

    def call_hooks(self, event: str, extra: dict[str, Any]) -> list[dict[str, Any]]:
        keys = CONTRACT["hook_inputs"]["PostToolUse_subagent" if "agent_id" in extra else event]
        base = {
            "session_id": self.session_id,
            "transcript_path": "",
            "cwd": str(Path.cwd()),
            "permission_mode": "bypassPermissions",
            "prompt_id": "prompt-1",
            "hook_event_name": event,
            "background_tasks": [],
            "session_crons": [],
            "last_assistant_message": "",
            "duration_ms": 1,
            **extra,
        }
        answers = []
        for callback_id in self.hooks.get(event, []):
            request_id = str(uuid.uuid4())
            self.emit(
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {
                        "subtype": "hook_callback",
                        "callback_id": callback_id,
                        "input": {key: base[key] for key in keys},
                        "tool_use_id": extra.get("tool_use_id"),
                    },
                }
            )
            answer = self.await_response(request_id)
            self.record("hook_answer", hook_event=event, answer=answer)
            answers.append(answer)
        return answers

    def await_response(self, request_id: str) -> dict[str, Any]:
        while (message := self.read()) is not None:
            response = message.get("response") or {}
            if message.get("type") == "control_response" and response.get("request_id") == request_id:
                return response.get("response") or {}
        return {}

    def hold(self, path: str, marker: str) -> None:
        self.record(marker)
        deadline = time.monotonic() + _HOLD_LIMIT_SECONDS
        while not Path(path).exists() and time.monotonic() < deadline:
            time.sleep(0.02)

    def assistant(self, block: dict[str, Any], *, parent: str | None = None) -> None:
        self.emit(
            {
                "type": "assistant",
                "message": {"model": "claude-fake", "role": "assistant", "content": [block]},
                "parent_tool_use_id": parent,
            }
        )

    def tool_step(self, step: dict[str, Any]) -> None:
        self.tool_ids += 1
        tool_id = f"toolu_fake_{self.tool_ids}"
        self.assistant({"type": "tool_use", "id": tool_id, "name": step["tool"], "input": {"command": "true"}})
        if step.get("hold_until"):
            self.hold(step["hold_until"], "holding_tool")
        extra: dict[str, Any] = {
            "tool_name": step["tool"],
            "tool_input": {"command": "true"},
            "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
            "tool_use_id": tool_id,
        }
        if step.get("subagent"):
            extra |= {"agent_id": "a-fake-subagent", "agent_type": "general-purpose"}
        self.call_hooks("PostToolUse", extra)
        self.emit(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": ""}],
                },
                "parent_tool_use_id": None,
            }
        )

    def stop_boundaries(self) -> None:
        active = False
        for _ in range(_MAX_STOP_CONTINUATIONS):
            reasons = [
                a["reason"]
                for a in self.call_hooks("Stop", {"stop_hook_active": active})
                if a.get("decision") == "block"
            ]
            if not reasons:
                return
            self.emit(
                {
                    "type": "user",
                    "isSynthetic": True,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": CONTRACT["stop_block_feedback_message"]["text_prefix"] + "\n".join(reasons),
                            }
                        ],
                    },
                    "parent_tool_use_id": None,
                }
            )
            self.turns += 1
            self.assistant({"type": "text", "text": self.script.get("final_text", "done")})
            active = True

    def run(self) -> None:
        while (message := self.read()) is not None and message.get("type") != "user":
            pass
        if message is None:
            return
        self.emit(
            {
                "type": "system",
                "subtype": "init",
                "model": "claude-fake",
                "claude_code_version": CONTRACT["claude_cli_version"],
            }
        )
        self.turns = 1
        for step in self.script.get("steps", []):
            if "tool" in step:
                self.tool_step(step)
            else:
                self.assistant({"type": "text", "text": step["text"]})
            self.turns += 1
        final_text = self.script.get("final_text", "done")
        self.assistant({"type": "text", "text": final_text})
        if self.script.get("hold_before_stop"):
            self.hold(self.script["hold_before_stop"], "holding_before_stop")
        self.stop_boundaries()
        if self.script.get("hold_after_stop"):
            self.hold(self.script["hold_after_stop"], "holding_after_stop")
        self.emit(
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 5,
                "duration_api_ms": 4,
                "is_error": False,
                "num_turns": self.turns,
                "result": final_text,
                "total_cost_usd": 0.0,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )
        self.record("result_emitted")
        while self.read() is not None:
            pass


def main() -> int:
    if sys.argv[1:] == ["-v"]:
        sys.stdout.write(f"{CONTRACT['claude_cli_version']} (Claude Code)\n")
        return 0
    FakeClaudeCli(json.loads(os.environ["T3_FAKE_CLAUDE_SCRIPT"]), Path(os.environ["T3_FAKE_CLAUDE_LOG"])).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
