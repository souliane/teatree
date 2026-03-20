"""Structured output schema for agent task results.

Agents return JSON matching this schema. Any agent that can produce JSON works —
Claude structured output just makes schema compliance guaranteed.
"""

from typing import TypedDict


class FileChange(TypedDict, total=False):
    path: str
    action: str  # "created", "modified", "deleted"
    lines_added: int
    lines_removed: int


class TestResult(TypedDict, total=False):
    name: str
    passed: bool
    duration_seconds: float
    error: str


class AgentResult(TypedDict, total=False):
    """Structured result from an agent task execution.

    All fields are optional — agents report what they can.
    """

    summary: str
    files_modified: list[FileChange]
    tests_run: list[TestResult]
    tests_passed: int
    tests_failed: int
    decisions: list[str]
    needs_user_input: bool
    user_input_reason: str
    next_steps: list[str]
    commands_executed: list[str]


RESULT_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One-line summary of what the agent did."},
        "files_modified": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "action": {"type": "string", "enum": ["created", "modified", "deleted"]},
                    "lines_added": {"type": "integer"},
                    "lines_removed": {"type": "integer"},
                },
                "required": ["path", "action"],
            },
        },
        "tests_run": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "passed": {"type": "boolean"},
                    "duration_seconds": {"type": "number"},
                    "error": {"type": "string"},
                },
                "required": ["name", "passed"],
            },
        },
        "tests_passed": {"type": "integer"},
        "tests_failed": {"type": "integer"},
        "decisions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Design decisions the agent made during execution.",
        },
        "needs_user_input": {"type": "boolean"},
        "user_input_reason": {"type": "string"},
        "next_steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Suggested follow-up actions.",
        },
        "commands_executed": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Shell commands the agent ran.",
        },
    },
    "additionalProperties": False,
}
