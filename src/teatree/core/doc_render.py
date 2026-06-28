import json
import re
from collections.abc import Callable
from inspect import signature
from pathlib import Path
from typing import TypedDict

from teatree.core.overlay import OverlayBase, OverlayMetadata
from teatree.skill_support.map import load_skill_delegation

_OVERLAY_HOOK_ORDER = (
    "get_repos",
    "get_provision_steps",
    "get_env_extra",
    "get_run_commands",
    "get_db_import_strategy",
    "get_post_db_steps",
    "get_symlinks",
    "get_services_config",
    "get_base_images",
    "get_docker_services",
    "reap_worktree_external_resources",
    "get_checking_sources",
)

_METADATA_HOOK_ORDER = (
    "validate_pr",
    "build_pr_title",
    "get_required_description_sections",
    "get_description_section_defaults",
    "get_skill_metadata",
    "get_ci_project_path",
    "get_e2e_config",
    "detect_variant",
    "get_tool_commands",
    "get_followup_repos",
)

_OVERLAY_HOOK_DESCRIPTIONS = {
    "get_repos": "Declare the repositories that TeaTree should provision for this overlay.",
    "get_provision_steps": "Return the ordered setup steps for a newly created worktree.",
    "get_env_extra": "Add overlay-specific environment variables to the generated worktree env file.",
    "get_run_commands": "Expose named service commands for `worktree start` and operator discovery.",
    "get_db_import_strategy": "Describe how a worktree database should be provisioned or restored.",
    "get_post_db_steps": "Return callbacks to run after database setup completes.",
    "get_symlinks": "Declare extra symlinks that should exist inside the worktree.",
    "get_services_config": "Return additional service metadata for worktree-lifecycle orchestration.",
    "get_base_images": "Declare Docker base images teatree builds once and shares across worktrees.",
    "get_docker_services": "Declare service names that MUST run in Docker — enforced at `worktree provision`.",
    "reap_worktree_external_resources": (
        "Reap a reaped worktree's out-of-band resources (e.g. its docker compose containers + images)."
    ),
    "get_checking_sources": "Return extra 'needs you' source identifiers for the `t3 <overlay> checking show` report.",
}

_METADATA_HOOK_DESCRIPTIONS = {
    "validate_pr": "Return PR validation problems for this overlay.",
    "build_pr_title": "Produce the PR title from structured ticket data (default: the commit subject).",
    "get_required_description_sections": (
        "Declare MR-description sections (beyond What/Why) the gate requires and the generator emits."
    ),
    "get_description_section_defaults": (
        "Map a required section to the default body the generator writes when the commit omits it."
    ),
    "get_skill_metadata": "Return the active overlay skill path and remote match patterns.",
    "get_ci_project_path": "Return the GitLab project path for CI operations.",
    "get_e2e_config": "Return E2E runner configuration (runner, test_dir, settings_module, project_path, ref).",
    "detect_variant": "Detect the current tenant variant from environment.",
    "get_tool_commands": "Return overlay-specific tool commands for t3 <overlay> tool.",
    "get_followup_repos": "Return GitLab project paths to sync MRs from.",
}

_OVERLAY_SETTINGS: tuple["SettingRecord", ...] = ()

_OVERLAY_COMMANDS = (
    "worktree provision",
    "worktree start",
    "worktree status",
    "worktree teardown",
    "tasks work-next-sdk",
    "followup refresh",
    "followup remind",
    "checking show",
)

_SKILL_METADATA_FIELDS: tuple["SkillFieldRecord", ...] = (
    {"name": "skill_path", "required": False, "description": "Primary overlay skill file path."},
    {
        "name": "remote_patterns",
        "required": False,
        "description": "Git remote patterns that activate the overlay skill outside the host project.",
    },
)

_TEATREE_RESPONSIBILITIES = (
    "Worktree lifecycle orchestration",
    "Task claiming, leasing, and execution routing",
    "Quality-gate state tracking on sessions",
    "Generated documentation surfaces",
)

_AGENT_LAUNCH_FIELDS = (
    "phase",
    "overlay_skill_path",
    "framework_skills",
    "lifecycle_skill",
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


class SkillCatalogueEntry(TypedDict):
    name: str
    summary: str


class SkillCataloguePayload(TypedDict):
    skills_root: str
    skills: list[SkillCatalogueEntry]


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.+?)\n---", re.DOTALL)
_TRIGGER_SPLIT_RE = re.compile(r"\.\s+Use (?:when|this|in)\b")


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
    for name in _METADATA_HOOK_ORDER:
        method = getattr(OverlayMetadata, name)
        hooks.append(
            {
                "name": f"metadata.{name}",
                "required": False,
                "signature": _signature_without_self(method),
                "description": _METADATA_HOOK_DESCRIPTIONS[name],
            },
        )
    return {
        "overlay_base": "teatree.core.overlay.OverlayBase",
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
    for hook in payload["hooks"]:
        sig = hook["signature"].replace("|", "\\|")
        required = "Yes" if hook["required"] else "No"
        lines.append(f"| `{hook['name']}` | {required} | `{sig}` | {hook['description']} |")
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


def build_skill_catalogue_payload(skills_root: Path) -> SkillCataloguePayload:
    """Read every ``skills/*/SKILL.md`` and return name + one-line summary.

    The summary is the description up to the first ``. Use when`` trigger
    clause, mirroring the README catalogue builder so the docs-site page and
    the README stay aligned with SKILL.md frontmatter (the SSOT).
    """
    entries: list[SkillCatalogueEntry] = []
    for skill_md in sorted(skills_root.glob("*/SKILL.md")):
        meta = _parse_frontmatter(skill_md)
        name = meta.get("name", skill_md.parent.name)
        description = meta.get("description", "")
        summary = _TRIGGER_SPLIT_RE.split(description, maxsplit=1)[0].rstrip(". ") if description else ""
        entries.append({"name": name, "summary": summary})
    return {"skills_root": skills_root.name, "skills": entries}


def render_skill_catalogue_markdown(payload: SkillCataloguePayload) -> str:
    lines = [
        "# Skills Catalogue",
        "",
        f"Source: `{payload['skills_root']}/*/SKILL.md` frontmatter",
        "",
        "| Skill | Summary |",
        "| --- | --- |",
    ]
    lines.extend(f"| `{entry['name']}` | {entry['summary'].replace('|', '\\|')} |" for entry in payload["skills"])
    return "\n".join(lines) + "\n"


def _parse_frontmatter(path: Path) -> dict[str, str]:
    match = _FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    if not match:
        return {}
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "-", "\t")):
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta


def write_generated_doc(
    json_path: Path,
    markdown_path: Path,
    payload: OverlayDocPayload | SkillDocPayload | SkillCataloguePayload,
    markdown: str,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")


def _signature_without_self(method: Callable[..., object]) -> str:
    raw_signature = str(signature(method))
    return raw_signature.replace("(self, ", "(").replace("(self)", "()")
