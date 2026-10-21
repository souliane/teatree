"""Transcript entries for a skill the agent loads mid-turn, in the shape Claude Code writes them."""

SKILL_BODY_TEXT = "Base directory for this skill: /src/skills/debug\n\n# Debug"


def skill_load_entries(tool_use_id: str = "toolu_skill") -> list[dict]:
    """The Skill tool call, its result, and the ``isMeta`` user entry carrying the skill body."""
    call = {"type": "tool_use", "id": tool_use_id, "name": "Skill", "input": {"skill": "t3:debug"}}
    result = {"type": "tool_result", "tool_use_id": tool_use_id, "content": "Launching skill: t3:debug"}
    return [
        {"type": "assistant", "message": {"role": "assistant", "content": [call]}},
        {"type": "user", "message": {"role": "user", "content": [result]}},
        {
            "type": "user",
            "isMeta": True,
            "sourceToolUseID": tool_use_id,
            "promptId": "p1",
            "message": {"role": "user", "content": [{"type": "text", "text": SKILL_BODY_TEXT}]},
        },
    ]


def slash_command_entries(command: str = "/t3:debug", args: str = "why is 4001 red?") -> list[dict]:
    """The owner's slash command, then the ``isMeta`` expansion the harness appends — no ``sourceToolUseID``."""
    typed = f"<command-message>debug</command-message>\n<command-name>{command}</command-name>\n"
    typed += f"<command-args>{args}</command-args>"
    return [
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": typed}]}},
        {
            "type": "user",
            "isMeta": True,
            "promptId": "p1",
            "message": {"role": "user", "content": [{"type": "text", "text": SKILL_BODY_TEXT}]},
        },
    ]
