"""The machine-readable capability registry (PR-30, front-end-seam keystone).

A front-end (Pi, a CI runner, another agent) drives teatree by shelling to
``t3 … --json``. Rather than scrape ``--help`` to discover which commands emit
JSON and how they exit, it reads ``t3 capabilities --json`` — this registry.

The registry is curated (not auto-derived from the live command tree: a
command's ``--json`` support and exit-code contract cannot be introspected
across the overlay subprocess bridge), and the ``teatree.cli.capabilities``
guard test keeps it honest against the commands PR-30 converts. Pure data — no
Django import — so ``t3 capabilities`` needs no DB bootstrap.
"""

import dataclasses
from typing import NotRequired, TypedDict

# The global exit-code contract every machine-drivable `t3` command honours.
# Subcommands raise `SystemExit(N)`; the overlay bridge propagates the child
# code faithfully (no traceback). A verdict command additionally maps a negative
# verdict onto a non-zero code so a driver can branch on `$?` alone.
EXIT_CODE_CONTRACT: dict[str, str] = {
    "0": "success",
    "1": "failure (runtime error, or a negative verdict on a verdict command)",
    "2": "usage / validation error (bad option, invalid argument)",
}

CAPABILITIES_VERSION = 1


@dataclasses.dataclass(frozen=True)
class Capability:
    """One machine-drivable command's JSON + exit-code contract."""

    command: str
    json_output: bool
    exit_codes: tuple[str, ...]
    note: str = ""


# The machine-driven lifecycle surface. `json_output` = the command emits a
# parseable JSON document on stdout (via `--json`, or always for a handoff
# command); the human view then goes to stderr. Ordered by group for readability.
CAPABILITIES: tuple[Capability, ...] = (
    # PR-30-converted lifecycle leaves (--json on stdout, human on stderr).
    Capability("teatree queue status", json_output=True, exit_codes=("0",)),
    Capability("teatree tasks list", json_output=True, exit_codes=("0",)),
    Capability(
        "teatree tasks create",
        json_output=True,
        exit_codes=("0", "1"),
        note="machine handoff: record JSON on stdout, human confirmation on stderr",
    ),
    Capability("teatree followup sync", json_output=True, exit_codes=("0",)),
    Capability("teatree worktree status", json_output=True, exit_codes=("0",)),
    Capability("teatree worktree diagnose", json_output=True, exit_codes=("0",)),
    Capability(
        "teatree worktree ready",
        json_output=False,
        exit_codes=("0", "1"),
        note="exit code IS the contract: 0 iff every readiness probe passes",
    ),
    Capability("teatree availability show", json_output=True, exit_codes=("0",)),
    Capability("teatree questions list", json_output=True, exit_codes=("0",)),
    Capability("teatree signals", json_output=True, exit_codes=("0",)),
    Capability(
        "teatree workspace emit",
        json_output=True,
        exit_codes=("0",),
        note="always JSON: the machine-readable clean-all handoff",
    ),
    Capability(
        "teatree do",
        json_output=True,
        exit_codes=("0", "1"),
        note="golden-path lifecycle walk: 0 = progress/pending/done, 1 = a gate blocked or ignored",
    ),
    # Pre-existing JSON commands (already machine-drivable before PR-30).
    Capability("teatree checking show", json_output=True, exit_codes=("0",)),
    Capability(
        "teatree e2e lanes", json_output=True, exit_codes=("0",), note="--json emits the {lane: [spec]} CI matrix"
    ),
    Capability("teatree env show", json_output=True, exit_codes=("0", "1"), note="--format json"),
    Capability("teatree db query", json_output=True, exit_codes=("0", "1")),
    Capability(
        "teatree workspace salvage",
        json_output=False,
        exit_codes=("0", "1"),
        note="human outcome line (salvaged/deleted/branch/pr), not a JSON document",
    ),
    Capability(
        "doctor check",
        json_output=True,
        exit_codes=("0", "1"),
        note="--json for the watchdog; --slack-roundtrip adds the deep live Slack round-trip probe (#3411)",
    ),
    Capability("cost", json_output=True, exit_codes=("0",)),
    Capability("tokens", json_output=True, exit_codes=("0",)),
    Capability("config show", json_output=True, exit_codes=("0",)),
    Capability("info artifacts", json_output=True, exit_codes=("0",), note="--format json"),
)


class CommandCapability(TypedDict):
    command: str
    json: bool
    exit_codes: list[str]
    note: NotRequired[str]


class CapabilitiesReport(TypedDict):
    version: int
    exit_code_contract: dict[str, str]
    commands: list[CommandCapability]


def capabilities_report() -> CapabilitiesReport:
    """The full registry as a JSON-serializable object for ``t3 capabilities --json``."""
    commands: list[CommandCapability] = []
    for cap in CAPABILITIES:
        entry: CommandCapability = {
            "command": cap.command,
            "json": cap.json_output,
            "exit_codes": list(cap.exit_codes),
        }
        if cap.note:
            entry["note"] = cap.note
        commands.append(entry)
    return {
        "version": CAPABILITIES_VERSION,
        "exit_code_contract": EXIT_CODE_CONTRACT,
        "commands": commands,
    }


__all__ = [
    "CAPABILITIES",
    "CAPABILITIES_VERSION",
    "EXIT_CODE_CONTRACT",
    "CapabilitiesReport",
    "Capability",
    "CommandCapability",
    "capabilities_report",
]
