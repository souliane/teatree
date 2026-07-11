"""Auto-generate a management-commands reference from the live Django command tree.

Pattern mirrors ``doc_render.py``: TypedDict payload + pure renderer + file
writer.  The management command ``generate_management_commands_doc`` calls this
module; ``generate_all_docs`` calls that command so the file is always
regenerated alongside other generated docs and covered by the existing
``docs-drift`` CI gate.
"""

import json
from pathlib import Path
from typing import TypedDict

import click
from django.core.management import get_commands, load_command_class
from typer.main import get_command


class SubcommandEntry(TypedDict):
    name: str
    help: str


class CommandEntry(TypedDict):
    name: str
    help: str
    subcommands: list[SubcommandEntry]


class ManagementCommandsDocPayload(TypedDict):
    commands: list[CommandEntry]


# Commands that exist as helper modules but are not real management commands.
_EXCLUDED = frozenset({"tasks_session_view"})

# App label that owns the commands we document.
_APP_LABEL = "teatree.core"


def build_management_commands_doc_payload() -> ManagementCommandsDocPayload:
    """Introspect every Django management command registered under ``teatree.core``.

    Returns a stable, deterministic payload (sorted by name) so the generated
    JSON is diff-friendly and idempotent across runs.
    """
    all_cmds = get_commands()
    core_names = sorted(name for name, app in all_cmds.items() if app == _APP_LABEL and name not in _EXCLUDED)

    commands: list[CommandEntry] = []
    for name in core_names:
        entry = _introspect_command(name)
        if entry is not None:
            commands.append(entry)

    return {"commands": commands}


def _introspect_command(name: str) -> CommandEntry | None:
    """Load and introspect a single management command, returning None on failure."""
    try:
        klass = load_command_class(_APP_LABEL, name)
    except Exception:  # noqa: BLE001 — a non-importable command is skipped in the doc build
        return None

    typer_app = getattr(klass, "typer_app", None)
    if typer_app is None:
        # Plain BaseCommand — just grab .help
        return {"name": name, "help": getattr(klass, "help", "") or "", "subcommands": []}

    try:
        click_app = get_command(typer_app)
    except Exception:  # noqa: BLE001 — a command whose Typer app can't be built degrades to a name-only entry
        return {"name": name, "help": getattr(klass, "help", "") or "", "subcommands": []}

    help_text = (click_app.help or "").strip()

    subcommands: list[SubcommandEntry] = []
    if isinstance(click_app, click.Group):
        ctx = click.Context(click_app)
        for sub_name in click_app.list_commands(ctx):
            sub = click_app.get_command(ctx, sub_name)
            if sub is None:
                continue
            sub_help = (sub.help or "").strip()
            # Use first sentence only for brevity.
            sub_help = sub_help.split("\n")[0].rstrip(". ")
            subcommands.append({"name": sub_name, "help": sub_help})

    return {"name": name, "help": help_text, "subcommands": subcommands}


def render_management_commands_markdown(payload: ManagementCommandsDocPayload) -> str:
    """Render the payload as a Markdown reference page."""
    lines: list[str] = [
        "# Management Commands",
        "",
        "Auto-generated from the live Django management command tree.",
        "Edit the source command, not this file.",
        "",
    ]

    for entry in payload["commands"]:
        lines.extend((f"## `{entry['name']}`", ""))
        if entry["help"]:
            # Only use the first paragraph of the help text.
            first_para = entry["help"].split("\n\n")[0].replace("\n", " ")
            lines.extend((first_para, ""))
        if entry["subcommands"]:
            lines.extend(("| Subcommand | Description |", "| --- | --- |"))
            for sub in entry["subcommands"]:
                desc = sub["help"].replace("|", "\\|")
                lines.append(f"| `{sub['name']}` | {desc} |")
            lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def write_management_commands_doc(output_dir: Path) -> tuple[Path, Path]:
    """Build the payload, render Markdown, and write both files.

    Returns ``(json_path, markdown_path)`` for callers that need to report them.
    """
    payload = build_management_commands_doc_payload()
    markdown = render_management_commands_markdown(payload)

    json_path = output_dir / "management-commands.json"
    markdown_path = output_dir / "management-commands.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return json_path, markdown_path
