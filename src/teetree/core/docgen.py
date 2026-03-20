import json
from collections.abc import Callable
from inspect import signature
from pathlib import Path
from typing import TypedDict

from teetree.core.overlay import OverlayBase
from teetree.skill_map import load_skill_delegation

_OVERLAY_HOOK_ORDER = (
    "get_repos",
    "get_provision_steps",
    "get_env_extra",
    "get_run_commands",
    "get_db_import_strategy",
    "get_post_db_steps",
    "get_symlinks",
    "get_services_config",
    "validate_mr",
    "get_skill_metadata",
)

_OVERLAY_HOOK_DESCRIPTIONS = {
    "get_repos": "Declare the repositories that TeaTree should provision for this overlay.",
    "get_provision_steps": "Return the ordered setup steps for a newly created worktree.",
    "get_env_extra": "Add overlay-specific environment variables to the generated worktree env file.",
    "get_run_commands": "Expose named service commands for lifecycle start and operator discovery.",
    "get_db_import_strategy": "Describe how a worktree database should be provisioned or restored.",
    "get_post_db_steps": "Return callbacks to run after database setup completes.",
    "get_symlinks": "Declare extra symlinks that should exist inside the worktree.",
    "get_services_config": "Return additional service metadata for lifecycle orchestration.",
    "validate_mr": "Return merge-request validation problems for this overlay.",
    "get_skill_metadata": "Return the active overlay skill path and any companion skills.",
}

_OVERLAY_SETTINGS: tuple["SettingRecord", ...] = (
    {
        "name": "TEATREE_OVERLAY_CLASS",
        "required": True,
        "description": "Import path for the active OverlayBase subclass.",
    },
    {
        "name": "TEATREE_HEADLESS_RUNTIME",
        "required": True,
        "description": "Runtime key for unattended SDK execution.",
    },
    {
        "name": "TEATREE_INTERACTIVE_RUNTIME",
        "required": True,
        "description": "Runtime key for interactive user-input work.",
    },
    {
        "name": "TEATREE_TERMINAL_MODE",
        "required": True,
        "description": "Terminal strategy used by the interactive runtime.",
    },
    {
        "name": "TEATREE_SDK_USE_CLI",
        "required": False,
        "description": "Use 'claude -p' for headless tasks instead of the Anthropic API. "
        "Set True when no API key is available.",
    },
    {
        "name": "TEATREE_CLAUDE_STATUSLINE_STATE_DIR",
        "required": False,
        "description": "Directory where Claude Code statusline integrations persist telemetry and session state.",
    },
    {
        "name": "TEATREE_AGENT_HANDOVER",
        "required": False,
        "description": "Ordered list of CLI runtimes used for handover priority, "
        "with optional telemetry providers and switch thresholds.",
    },
)

_OVERLAY_COMMANDS = (
    "lifecycle setup",
    "lifecycle start",
    "lifecycle status",
    "lifecycle teardown",
    "tasks work-next-sdk",
    "tasks work-next-user-input",
    "followup refresh",
    "followup remind",
)

_SKILL_METADATA_FIELDS: tuple["SkillFieldRecord", ...] = (
    {"name": "skill_path", "required": False, "description": "Primary overlay skill file path."},
    {
        "name": "companion_skills",
        "required": False,
        "description": "Additional skills loaded alongside the primary overlay skill.",
    },
)

_TEATREE_RESPONSIBILITIES = (
    "Worktree lifecycle orchestration",
    "Task claiming, leasing, and execution routing",
    "Quality-gate state tracking on sessions",
    "Generated dashboard and documentation surfaces",
)

_AGENT_LAUNCH_FIELDS = (
    "phase",
    "overlay_skill_path",
    "companion_skills",
    "delegated_skills",
)


class HookRecord(TypedDict):
    name: str
    required: bool
    signature: str
    description: str


class SettingRecord(TypedDict):
    name: str
    required: bool
    description: str


class SkillFieldRecord(TypedDict):
    name: str
    required: bool
    description: str


class OverlayDocPayload(TypedDict):
    overlay_base: str
    hooks: list[HookRecord]
    settings: list[SettingRecord]
    commands: list[str]
    skill_metadata_fields: list[SkillFieldRecord]


class SkillDocPayload(TypedDict):
    skill_map_path: str
    delegation: dict[str, list[str]]
    teatree_responsibilities: list[str]
    agent_launch_fields: list[str]


def build_overlay_doc_payload() -> OverlayDocPayload:
    hooks: list[HookRecord] = []
    for name in _OVERLAY_HOOK_ORDER:
        method = getattr(OverlayBase, name)
        hooks.append(
            {
                "name": name,
                "required": bool(getattr(method, "__isabstractmethod__", False)),
                "signature": _signature_without_self(method),
                "description": _OVERLAY_HOOK_DESCRIPTIONS[name],
            },
        )
    return {
        "overlay_base": "teetree.core.overlay.OverlayBase",
        "hooks": hooks,
        "settings": list(_OVERLAY_SETTINGS),
        "commands": list(_OVERLAY_COMMANDS),
        "skill_metadata_fields": list(_SKILL_METADATA_FIELDS),
    }


def render_overlay_markdown(payload: OverlayDocPayload) -> str:
    lines = [
        "# Overlay Extension Points",
        "",
        f"Base class: `{payload['overlay_base']}`",
        "",
        "## Hooks",
        "",
        "| Hook | Required | Signature | Description |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| `{hook['name']}` | {'Yes' if hook['required'] else 'No'} | `{hook['signature']}` | {hook['description']} |"
        for hook in payload["hooks"]
    )
    lines.extend(
        [
            "",
            "## Settings",
            "",
            "| Setting | Required | Description |",
            "| --- | --- | --- |",
        ],
    )
    lines.extend(
        f"| `{setting['name']}` | {'Yes' if setting['required'] else 'No'} | {setting['description']} |"
        for setting in payload["settings"]
    )
    lines.extend(
        [
            "",
            "## Runtime Commands",
            "",
        ],
    )
    lines.extend(f"- `{command}`" for command in payload["commands"])
    lines.extend(
        [
            "",
            "## Skill Metadata",
            "",
            "| Field | Required | Description |",
            "| --- | --- | --- |",
        ],
    )
    lines.extend(
        f"| `{field['name']}` | {'Yes' if field['required'] else 'No'} | {field['description']} |"
        for field in payload["skill_metadata_fields"]
    )
    return "\n".join(lines) + "\n"


def build_skill_doc_payload(skill_map_path: Path | None) -> SkillDocPayload:
    source_path, mapping = load_skill_delegation(skill_map_path)
    return {
        "skill_map_path": source_path,
        "delegation": mapping,
        "teatree_responsibilities": list(_TEATREE_RESPONSIBILITIES),
        "agent_launch_fields": list(_AGENT_LAUNCH_FIELDS),
    }


def render_skill_markdown(payload: SkillDocPayload) -> str:
    lines = [
        "# Skill Delegation Matrix",
        "",
        f"Source: `{payload['skill_map_path']}`",
        "",
        "## Delegation",
        "",
        "| Phase | Delegated Skills |",
        "| --- | --- |",
    ]
    for phase, skills in payload["delegation"].items():
        lines.append(f"| `{phase}` | {', '.join(f'`{skill}`' for skill in skills)} |")
    lines.extend(
        [
            "",
            "## TeaTree Responsibilities Retained Locally",
            "",
        ],
    )
    lines.extend(f"- {item}" for item in payload["teatree_responsibilities"])
    lines.extend(
        [
            "",
            "## Agent Launch Fields",
            "",
        ],
    )
    lines.extend(f"- `{field}`" for field in payload["agent_launch_fields"])
    return "\n".join(lines) + "\n"


def write_generated_doc(
    json_path: Path,
    markdown_path: Path,
    payload: OverlayDocPayload | SkillDocPayload,
    markdown: str,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")


def _signature_without_self(method: Callable[..., object]) -> str:
    raw_signature = str(signature(method))
    return raw_signature.replace("(self, ", "(").replace("(self)", "()")
