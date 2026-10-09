"""Atomic codec for the cached skills.sh inventory receipt."""

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from teatree.harness_skills import SkillsHarness
from teatree.provisioning.skill_provenance import GitProbeState, GitProvenance, SkillInstallationFact, SkillInstallKind
from teatree.provisioning.skills_cli import (
    SKILLS_CLI_VERSION,
    SKILLS_RECEIPT_VERSION,
    HarnessSkillInventory,
    InstalledSkill,
    SkillsCliSchemaError,
    SkillsInventoryReceipt,
    SkillsInventoryReceiptError,
    _parse_installed_skill,
)

type JsonObject = dict[str, object]


def _receipt_error(detail: str) -> SkillsInventoryReceiptError:
    return SkillsInventoryReceiptError(detail)


def read_inventory_receipt(path: Path) -> SkillsInventoryReceipt:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        detail = "skills inventory receipt is not valid JSON"
        raise _receipt_error(detail) from error
    return _parse_receipt(payload)


def _installed_skill_payload(skill: InstalledSkill) -> JsonObject:
    return {
        "name": skill.name,
        "path": skill.path,
        "scope": skill.scope,
        "agents": list(skill.agents),
        "source": skill.source,
        "sourceUrl": skill.source_url,
        "sourceType": skill.source_type,
        "installation": _installation_payload(skill.installation),
    }


def _installation_payload(installation: SkillInstallationFact | None) -> JsonObject | None:
    if installation is None:
        return None
    return {
        "frontDoorPath": installation.front_door_path,
        "kind": installation.kind.value,
        "linkTarget": installation.link_target,
        "git": _git_payload(installation.git),
    }


def _git_payload(git: GitProvenance | None) -> JsonObject | None:
    if git is None:
        return None
    return {
        "state": git.state.value,
        "repoPath": git.repo_path,
        "localSha": git.local_sha,
        "branch": git.branch,
        "detached": git.detached,
        "defaultBranch": git.default_branch,
        "defaultSha": git.default_sha,
        "ahead": git.ahead,
        "behind": git.behind,
        "newerOnDefault": git.newer_on_default,
        "diagnostic": git.diagnostic,
    }


def _receipt_payload(receipt: SkillsInventoryReceipt) -> JsonObject:
    return {
        "schemaVersion": receipt.schema_version,
        "generatedAt": receipt.generated_at.isoformat(),
        "cliVersion": receipt.cli_version,
        "inventories": {
            inventory.harness.value: [_installed_skill_payload(skill) for skill in inventory.skills]
            for inventory in receipt.inventories
        },
    }


def write_inventory_receipt(path: Path, receipt: SkillsInventoryReceipt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, staged_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    staged = Path(staged_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(_receipt_payload(receipt), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(path)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _parse_receipt(value: object) -> SkillsInventoryReceipt:
    if not isinstance(value, dict):
        detail = "skills inventory receipt must be an object"
        raise _receipt_error(detail)
    row: Mapping[str, object] = value
    if row.get("schemaVersion") != SKILLS_RECEIPT_VERSION:
        detail = "unsupported skills inventory receipt version"
        raise _receipt_error(detail)
    generated_at = _parse_generated_at(row.get("generatedAt"))
    if row.get("cliVersion") != SKILLS_CLI_VERSION:
        detail = "skills inventory receipt CLI version mismatch"
        raise _receipt_error(detail)
    inventory_values = row.get("inventories")
    if not isinstance(inventory_values, dict):
        detail = "skills inventory receipt inventories must be an object"
        raise _receipt_error(detail)
    if set(inventory_values) != {harness.value for harness in SkillsHarness}:
        detail = "skills inventory receipt must contain every supported harness"
        raise _receipt_error(detail)
    try:
        inventories = tuple(
            HarnessSkillInventory(
                harness=harness,
                skills=tuple(_parse_receipt_skill(item) for item in _receipt_inventory(inventory_values, harness)),
            )
            for harness in SkillsHarness
        )
    except SkillsCliSchemaError as error:
        detail = "skills inventory receipt contains an invalid skill"
        raise _receipt_error(detail) from error
    return SkillsInventoryReceipt(
        schema_version=SKILLS_RECEIPT_VERSION,
        generated_at=generated_at,
        cli_version=SKILLS_CLI_VERSION,
        inventories=inventories,
    )


def _parse_generated_at(value: object) -> datetime:
    if not isinstance(value, str):
        detail = "skills inventory generated time must be an ISO timestamp"
        raise _receipt_error(detail)
    try:
        generated_at = datetime.fromisoformat(value)
    except ValueError as error:
        detail = "skills inventory generated time must be an ISO timestamp"
        raise _receipt_error(detail) from error
    if generated_at.tzinfo is None:
        detail = "skills inventory generated time must include a timezone"
        raise _receipt_error(detail)
    return generated_at


def _receipt_inventory(inventories: Mapping[str, object], harness: SkillsHarness) -> list[object]:
    value = inventories[harness.value]
    if not isinstance(value, list):
        detail = "skills inventory receipt harness entries must be lists"
        raise _receipt_error(detail)
    return value


def _parse_receipt_skill(value: object) -> InstalledSkill:
    skill = _parse_installed_skill(value)
    if not isinstance(value, dict):
        detail = "skills inventory receipt skill must be an object"
        raise _receipt_error(detail)
    return replace(skill, installation=_parse_installation(value.get("installation")))


def _parse_installation(value: object) -> SkillInstallationFact:
    if not isinstance(value, dict):
        detail = "skills inventory receipt installation must be an object"
        raise _receipt_error(detail)
    row: Mapping[str, object] = value
    front_door = row.get("frontDoorPath")
    kind_value = row.get("kind")
    link_target = row.get("linkTarget")
    if not isinstance(front_door, str) or not front_door:
        detail = "skills inventory front door path must be a nonempty string"
        raise _receipt_error(detail)
    if not isinstance(kind_value, str):
        detail = "skills inventory install kind must be a string"
        raise _receipt_error(detail)
    try:
        kind = SkillInstallKind(kind_value)
    except ValueError as error:
        detail = "skills inventory install kind is unknown"
        raise _receipt_error(detail) from error
    if link_target is not None and not isinstance(link_target, str):
        detail = "skills inventory link target must be a string or null"
        raise _receipt_error(detail)
    return SkillInstallationFact(
        front_door_path=front_door,
        kind=kind,
        link_target=link_target,
        git=_parse_git(row.get("git")),
    )


def _parse_git(value: object) -> GitProvenance | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        detail = "skills inventory git provenance must be an object or null"
        raise _receipt_error(detail)
    row: Mapping[str, object] = value
    state_value = row.get("state")
    if not isinstance(state_value, str):
        detail = "skills inventory git state must be a string"
        raise _receipt_error(detail)
    try:
        state = GitProbeState(state_value)
    except ValueError as error:
        detail = "skills inventory git state is unknown"
        raise _receipt_error(detail) from error
    return GitProvenance(
        state=state,
        repo_path=_nullable_string(row, "repoPath"),
        local_sha=_nullable_string(row, "localSha"),
        branch=_nullable_string(row, "branch"),
        detached=_nullable_bool(row, "detached"),
        default_branch=_nullable_string(row, "defaultBranch"),
        default_sha=_nullable_string(row, "defaultSha"),
        ahead=_nullable_int(row, "ahead"),
        behind=_nullable_int(row, "behind"),
        newer_on_default=_nullable_bool(row, "newerOnDefault"),
        diagnostic=_nullable_string(row, "diagnostic"),
    )


def _nullable_string(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is not None and not isinstance(value, str):
        detail = f"skills inventory `{key}` must be a string or null"
        raise _receipt_error(detail)
    return value


def _nullable_bool(row: Mapping[str, object], key: str) -> bool | None:
    value = row.get(key)
    if value is not None and not isinstance(value, bool):
        detail = f"skills inventory `{key}` must be a boolean or null"
        raise _receipt_error(detail)
    return value


def _nullable_int(row: Mapping[str, object], key: str) -> int | None:
    value = row.get(key)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        detail = f"skills inventory `{key}` must be an integer or null"
        raise _receipt_error(detail)
    return value
