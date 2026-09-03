"""Parameter schemas for the built-in tools an eval scenario registers as a stub.

The sibling of :mod:`teatree.llm.builtin_tools`: that module answers *which* tool
names the bundled ``claude`` CLI knows, this one answers *what arguments each of
them takes*. The eval lanes that register a scenario's declared tools as inert
stubs (:func:`teatree.eval.pydantic_ai_runner.build_eval_toolset`) advertise these
schemas so the model is told the same argument shape the production tool declares.

Without it a stub whose only parameter is ``**kwargs`` advertises ZERO properties:
the model is never told what any tool takes, so it emits ``AskUserQuestion({})`` —
it cannot invent a structured shape it was not shown — and every matcher grading
``args.questions`` reds an agent that behaved correctly.

The shapes describe the PRODUCTION tools, transcribed from the interfaces the CLI
itself advertises and corroborated against the ``tool_use`` inputs recorded in
``evals/fixtures/*.stream.jsonl`` by real Claude Code runs. They are deliberately
NOT derived from what any scenario asserts: deriving them from matcher ``args.*``
paths would leak the rubric into the prompt and let a scenario pass by telling the
model what is being graded. That independence is checkable — every entry carries
arguments (``Bash.timeout``, ``Edit.replace_all``, ``Read.offset``, …) that no
matcher in the catalog mentions.

A name with no entry keeps the fully permissive stub, and every entry leaves
``additionalProperties`` open, so an unmodelled argument still reaches the call.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic.json_schema import JsonSchemaValue

_STRING: JsonSchemaValue = {"type": "string"}
_NUMBER: JsonSchemaValue = {"type": "number"}
_BOOLEAN: JsonSchemaValue = {"type": "boolean"}
_STRINGS: JsonSchemaValue = {"type": "array", "items": _STRING}


def _array_of(item: JsonSchemaValue) -> JsonSchemaValue:
    return {"type": "array", "items": item}


def _object(properties: Mapping[str, JsonSchemaValue], required: tuple[str, ...]) -> JsonSchemaValue:
    return {"type": "object", "properties": dict(properties), "required": list(required)}


@dataclass(frozen=True, slots=True)
class ToolParameters:
    """One built-in tool's declared arguments, rendered as a JSON Schema object."""

    properties: Mapping[str, JsonSchemaValue]
    required: tuple[str, ...] = ()

    def json_schema(self) -> JsonSchemaValue:
        schema: JsonSchemaValue = {
            "type": "object",
            "properties": dict(self.properties),
            "additionalProperties": True,
        }
        if self.required:
            schema["required"] = list(self.required)
        return schema


_ASK_OPTION = _object({"label": _STRING, "description": _STRING}, ("label",))
_ASK_QUESTION = _object(
    {"question": _STRING, "header": _STRING, "options": _array_of(_ASK_OPTION), "multiSelect": _BOOLEAN},
    ("question", "header", "options", "multiSelect"),
)
_TODO = _object({"content": _STRING, "status": _STRING, "activeForm": _STRING}, ("content", "status", "activeForm"))
_SUBAGENT = ToolParameters(
    properties={"description": _STRING, "prompt": _STRING, "subagent_type": _STRING},
    required=("description", "prompt", "subagent_type"),
)

#: The curated map, covering every tool name the scenario catalog declares.
BUILTIN_TOOL_PARAMETERS: Mapping[str, ToolParameters] = {
    "Agent": _SUBAGENT,
    "AskUserQuestion": ToolParameters({"questions": _array_of(_ASK_QUESTION)}, ("questions",)),
    "Bash": ToolParameters(
        {"command": _STRING, "description": _STRING, "timeout": _NUMBER, "run_in_background": _BOOLEAN},
        ("command",),
    ),
    "Edit": ToolParameters(
        {"file_path": _STRING, "old_string": _STRING, "new_string": _STRING, "replace_all": _BOOLEAN},
        ("file_path", "old_string", "new_string"),
    ),
    "Glob": ToolParameters({"pattern": _STRING, "path": _STRING}, ("pattern",)),
    "Grep": ToolParameters(
        {
            "pattern": _STRING,
            "path": _STRING,
            "glob": _STRING,
            "type": _STRING,
            "output_mode": _STRING,
            "head_limit": _NUMBER,
            "multiline": _BOOLEAN,
        },
        ("pattern",),
    ),
    "Monitor": ToolParameters(
        {"command": _STRING, "description": _STRING, "timeout_ms": _NUMBER, "persistent": _BOOLEAN},
        ("description",),
    ),
    "PushNotification": ToolParameters({"message": _STRING, "status": _STRING}, ("message", "status")),
    "Read": ToolParameters({"file_path": _STRING, "offset": _NUMBER, "limit": _NUMBER}, ("file_path",)),
    "Skill": ToolParameters({"skill": _STRING, "args": _STRING}, ("skill",)),
    "Task": _SUBAGENT,
    "TaskCreate": ToolParameters({"subject": _STRING, "description": _STRING}, ("subject",)),
    "TaskList": ToolParameters({"status": _STRING}),
    "TodoWrite": ToolParameters({"todos": _array_of(_TODO)}, ("todos",)),
    "WebFetch": ToolParameters({"url": _STRING, "prompt": _STRING}, ("url", "prompt")),
    "WebSearch": ToolParameters(
        {"query": _STRING, "allowed_domains": _STRINGS, "blocked_domains": _STRINGS},
        ("query",),
    ),
    "Write": ToolParameters({"file_path": _STRING, "content": _STRING}, ("file_path", "content")),
}

__all__ = ["BUILTIN_TOOL_PARAMETERS", "ToolParameters"]
