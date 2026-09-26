import fcntl
import json
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from teatree.harness_skills import HarnessSkillPolicyError, SkillsHarness, normalize_harness_skill_name
from teatree.provisioning.skill_provenance import SkillInstallationFact, SkillInstallationProber, SkillProvenanceProbe
from teatree.utils.run import CompletedProcess, run_allowed_to_fail

SKILLS_CLI_VERSION = "1.7.0"
SKILLS_RECEIPT_VERSION = 2


class SkillAddStatus(StrEnum):
    INSTALLED = "installed"
    SKIPPED = "skipped"
    FAILED = "failed"


class SkillsCliError(RuntimeError):
    pass


class SkillsCliUnavailableError(SkillsCliError):
    def __init__(self, command: tuple[str, ...]) -> None:
        self.command = command
        super().__init__(
            f"`{command[0]}` is unavailable; install skills@{SKILLS_CLI_VERSION} or rebuild the TeaTree deploy image"
        )


class SkillsCliCommandError(SkillsCliError):
    def __init__(self, command: tuple[str, ...], returncode: int, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr or "no stderr"
        super().__init__(f"skills command failed with exit code {returncode}: {detail}")


class SkillsCliVersionError(SkillsCliError):
    def __init__(self, actual: str) -> None:
        self.expected = SKILLS_CLI_VERSION
        self.actual = actual
        super().__init__(f"skills CLI version mismatch: expected {self.expected}, got {actual or 'empty output'}")


class SkillsCliSchemaError(SkillsCliError):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"invalid skills CLI output: {detail}")


class SkillsCliRemovalError(SkillsCliError):
    def __init__(self, name: str, harness: SkillsHarness, detail: str) -> None:
        self.name = name
        self.harness = harness
        self.detail = detail
        super().__init__(f"could not remove {name!r} from {harness.value}: {detail}")


class SkillsCliInputError(ValueError):
    def __init__(self, expectation: str) -> None:
        self.expectation = expectation
        super().__init__(f"skills CLI input must specify {expectation}")


class SkillsInventoryReceiptError(RuntimeError):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"invalid skills inventory receipt: {detail}")


class UnsupportedSkillsHarnessError(TypeError):
    def __init__(self, harness: object) -> None:
        self.harness = harness
        super().__init__(f"unsupported skills harness: {harness}")


def _cli_schema_error(detail: str) -> SkillsCliSchemaError:
    return SkillsCliSchemaError(detail)


def _cli_input_error(expectation: str) -> SkillsCliInputError:
    return SkillsCliInputError(expectation)


def _receipt_error(detail: str) -> SkillsInventoryReceiptError:
    return SkillsInventoryReceiptError(detail)


@dataclass(frozen=True, slots=True)
class InstalledSkill:
    name: str
    path: str
    scope: str
    agents: tuple[str, ...]
    source: str | None
    source_url: str | None
    source_type: str | None
    installation: SkillInstallationFact | None = None


@dataclass(frozen=True, slots=True)
class SkillAddResult:
    name: str
    status: SkillAddStatus


@dataclass(frozen=True, slots=True)
class HarnessSkillInventory:
    harness: SkillsHarness
    skills: tuple[InstalledSkill, ...]


@dataclass(frozen=True, slots=True)
class SkillsInventoryReceipt:
    schema_version: int
    generated_at: datetime
    cli_version: str
    inventories: tuple[HarnessSkillInventory, ...]


CommandRunner = Callable[[Sequence[str]], CompletedProcess[str]]


def _run_command(command: Sequence[str]) -> CompletedProcess[str]:
    return run_allowed_to_fail(command, expected_codes=None)


@dataclass(slots=True)
class SkillsCli:
    binary: str = "skills"
    runner: CommandRunner = _run_command
    home: Path | None = None

    def version(self) -> str:
        actual = self._invoke("--version").stdout.strip()
        if actual != SKILLS_CLI_VERSION:
            raise SkillsCliVersionError(actual)
        return actual

    def list_global(self, harness: SkillsHarness, *, home: Path | None = None) -> tuple[InstalledSkill, ...]:
        harness_value = _harness_value(harness)
        result = self._invoke("list", "-g", "-a", harness_value, "--json")
        return _inventory_for_harness(_parse_inventory(result.stdout), harness, home or self.home or Path.home())

    def remove_global(self, name: str, harness: SkillsHarness, *, home: Path | None = None) -> None:
        skill_name = _concrete_skill_value(name)
        harness_value = _harness_value(harness)
        home_path = home or self.home or Path.home()
        if harness is SkillsHarness.CODEX:
            _preserve_claude_copy(home_path, skill_name)
        with _preserve_codex_installation(home_path, skill_name, enabled=harness is SkillsHarness.CLAUDE_CODE):
            self._invoke("remove", skill_name, "-g", "-a", harness_value, "-y")
        try:
            if harness is SkillsHarness.CODEX:
                for path in _codex_front_door_paths(home_path, skill_name):
                    _remove_path(path)
            remaining = tuple(
                path for path in _harness_front_door_paths(home_path, harness, skill_name) if _path_exists(path)
            )
        except OSError as error:
            raise SkillsCliRemovalError(skill_name, harness, "front-door cleanup failed") from error
        if remaining:
            raise SkillsCliRemovalError(skill_name, harness, "the harness front door is still present")

    def add_selected(
        self,
        package: str,
        harnesses: Sequence[SkillsHarness],
        skills: Sequence[str],
    ) -> tuple[SkillAddResult, ...]:
        package_value = _concrete_value(package, "package")
        if not harnesses:
            expectation = "at least one harness"
            raise _cli_input_error(expectation)
        harness_values = tuple(_harness_value(harness) for harness in harnesses)
        skill_values = _selected_skill_values(skills)
        result = self._invoke(
            "add",
            package_value,
            "-g",
            "-a",
            *harness_values,
            "-s",
            *skill_values,
            *(("--copy",) if harnesses == (SkillsHarness.CLAUDE_CODE,) else ()),
            "-y",
            "--json",
        )
        return _parse_add_results(result.stdout)

    def _invoke(self, *arguments: str) -> CompletedProcess[str]:
        command = (self.binary, *arguments)
        try:
            result = self.runner(command)
        except OSError as error:
            raise SkillsCliUnavailableError(command) from error
        if result.returncode != 0:
            raise SkillsCliCommandError(command, result.returncode, result.stderr.strip())
        return result


def _harness_value(harness: SkillsHarness) -> str:
    if not isinstance(harness, SkillsHarness):
        raise UnsupportedSkillsHarnessError(harness)
    return harness.value


def _concrete_value(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized or normalized == "*" or normalized.startswith("-"):
        expectation = f"a concrete {label}"
        raise _cli_input_error(expectation)
    return normalized


def _selected_skill_values(skills: Sequence[str]) -> tuple[str, ...]:
    if not skills:
        expectation = "selected skills containing concrete skill names"
        raise _cli_input_error(expectation)
    try:
        return tuple(_concrete_skill_value(skill) for skill in skills)
    except SkillsCliInputError as error:
        expectation = "selected skills containing only concrete skill names"
        raise _cli_input_error(expectation) from error


def _concrete_skill_value(value: str) -> str:
    try:
        return normalize_harness_skill_name(value)
    except HarnessSkillPolicyError as error:
        expectation = "a concrete skill name"
        raise _cli_input_error(expectation) from error


def _parse_json_list(stdout: str) -> list[object]:
    decoder = json.JSONDecoder()
    payload: object | None = None
    last_error: json.JSONDecodeError | None = None
    for offset in reversed([index for index, character in enumerate(stdout) if character in "[{"]):
        try:
            candidate, end = decoder.raw_decode(stdout, offset)
        except json.JSONDecodeError as error:
            last_error = error
            continue
        if not stdout[end:].strip():
            payload = candidate
            break
    if payload is None:
        detail = "skills CLI stdout has no final JSON value"
        raise _cli_schema_error(detail) from last_error
    if not isinstance(payload, list):
        detail = "skills CLI JSON output must be a list"
        raise _cli_schema_error(detail)
    return payload


def _required_string(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        detail = f"skills CLI JSON field `{key}` must be a nonempty string"
        raise _cli_schema_error(detail)
    return value


def _optional_string(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        detail = f"skills CLI JSON field `{key}` must be a string or null"
        raise _cli_schema_error(detail)
    return value


def _parse_installed_skill(value: object) -> InstalledSkill:
    if not isinstance(value, dict):
        detail = "skills CLI inventory entries must be objects"
        raise _cli_schema_error(detail)
    row: Mapping[str, object] = value
    scope = _required_string(row, "scope")
    if scope != "global":
        detail = "skills CLI global inventory contains a non-global entry"
        raise _cli_schema_error(detail)
    agents = row.get("agents")
    if not isinstance(agents, list) or not all(isinstance(agent, str) for agent in agents):
        detail = "skills CLI JSON field `agents` must be a string list"
        raise _cli_schema_error(detail)
    return InstalledSkill(
        name=_required_string(row, "name"),
        path=_required_string(row, "path"),
        scope=scope,
        agents=tuple(agents),
        source=_optional_string(row, "source"),
        source_url=_optional_string(row, "sourceUrl"),
        source_type=_optional_string(row, "sourceType"),
    )


def _parse_inventory(stdout: str) -> tuple[InstalledSkill, ...]:
    return tuple(_parse_installed_skill(value) for value in _parse_json_list(stdout))


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _codex_front_door_paths(home: Path, name: str) -> tuple[Path, Path]:
    return home / ".agents" / "skills" / name, home / ".codex" / "skills" / name


def _harness_front_door_paths(home: Path, harness: SkillsHarness, name: str) -> tuple[Path, ...]:
    if harness is SkillsHarness.CLAUDE_CODE:
        return (home / ".claude" / "skills" / name,)
    return _codex_front_door_paths(home, name)


def _installed_front_door(home: Path, harness: SkillsHarness, skill: InstalledSkill) -> Path | None:
    if harness is SkillsHarness.CLAUDE_CODE:
        candidate = home / ".claude" / "skills" / skill.name
        return candidate if _path_exists(candidate) else None
    reported = Path(skill.path).expanduser()
    return reported if reported in _codex_front_door_paths(home, skill.name) and _path_exists(reported) else None


def _inventory_for_harness(
    skills: tuple[InstalledSkill, ...],
    harness: SkillsHarness,
    home: Path,
) -> tuple[InstalledSkill, ...]:
    selected: list[InstalledSkill] = []
    seen: set[Path] = set()
    for skill in skills:
        front_door = _installed_front_door(home, harness, skill)
        if front_door is None or front_door in seen:
            continue
        seen.add(front_door)
        selected.append(skill)
    return tuple(selected)


def _preserve_claude_copy(home: Path, name: str) -> None:
    universal = home / ".agents" / "skills" / name
    claude = home / ".claude" / "skills" / name
    if not universal.is_dir() or not claude.is_symlink():
        return
    try:
        if claude.resolve(strict=True) != universal.resolve(strict=True):
            return
        claude.parent.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=f".{name}-", suffix=".copy", dir=claude.parent))
    except OSError as error:
        raise SkillsCliRemovalError(name, SkillsHarness.CODEX, "could not prepare the Claude Code copy") from error
    try:
        staged.rmdir()
        shutil.copytree(universal, staged, symlinks=True)
        claude.unlink()
        staged.replace(claude)
    except OSError as error:
        _remove_path(staged)
        raise SkillsCliRemovalError(name, SkillsHarness.CODEX, "could not preserve the Claude Code copy") from error


@contextmanager
def _preserve_codex_installation(home: Path, name: str, *, enabled: bool) -> Iterator[None]:
    """Restore Codex's shared front door if a Claude-only CLI removal deletes it."""
    universal = home / ".agents" / "skills" / name
    if not enabled or not _path_exists(universal):
        yield
        return
    backup_root: Path | None = None
    link_target: Path | None = None
    try:
        home.mkdir(parents=True, exist_ok=True)
        backup_root = Path(tempfile.mkdtemp(prefix=".t3-codex-skill-", dir=home))
        if universal.is_symlink():
            link_target = universal.readlink()
        else:
            shutil.copytree(universal, backup_root / "skill", symlinks=True)
    except OSError as error:
        if backup_root is not None:
            shutil.rmtree(backup_root, ignore_errors=True)
        raise SkillsCliRemovalError(name, SkillsHarness.CLAUDE_CODE, "could not preserve the Codex copy") from error
    try:
        yield
    finally:
        try:
            if not _path_exists(universal):
                universal.parent.mkdir(parents=True, exist_ok=True)
                if link_target is not None:
                    universal.symlink_to(link_target, target_is_directory=True)
                else:
                    (backup_root / "skill").replace(universal)
        except OSError as error:
            raise SkillsCliRemovalError(
                name,
                SkillsHarness.CLAUDE_CODE,
                "could not restore the Codex front door",
            ) from error
        finally:
            shutil.rmtree(backup_root, ignore_errors=True)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _parse_add_result(value: object) -> SkillAddResult:
    if not isinstance(value, dict):
        detail = "skills CLI add result entries must be objects"
        raise _cli_schema_error(detail)
    row: Mapping[str, object] = value
    status_value = _required_string(row, "status")
    try:
        status = SkillAddStatus(status_value)
    except ValueError as error:
        detail = f"unknown skills CLI add status: {status_value}"
        raise _cli_schema_error(detail) from error
    return SkillAddResult(name=_required_string(row, "name"), status=status)


def _parse_add_results(stdout: str) -> tuple[SkillAddResult, ...]:
    return tuple(_parse_add_result(value) for value in _parse_json_list(stdout))


def refresh_inventory_receipt(
    path: Path,
    *,
    cli: SkillsCli | None = None,
    generated_at: datetime | None = None,
    home: Path | None = None,
    provenance_probe: SkillInstallationProber | None = None,
) -> SkillsInventoryReceipt:
    with _inventory_refresh_lock(path):
        client = cli or SkillsCli()
        probe = provenance_probe or SkillProvenanceProbe()
        home_path = home or Path.home()
        cli_version = client.version()
        inventories = tuple(
            HarnessSkillInventory(
                harness=harness,
                skills=tuple(
                    replace(skill, installation=probe.probe(front_door))
                    for skill in client.list_global(harness, home=home_path)
                    if (front_door := _installed_front_door(home_path, harness, skill)) is not None
                ),
            )
            for harness in SkillsHarness
        )
        moment = generated_at or datetime.now(UTC)
        if moment.tzinfo is None:
            detail = "skills inventory generated time must include a timezone"
            raise _receipt_error(detail)
        receipt = SkillsInventoryReceipt(
            schema_version=SKILLS_RECEIPT_VERSION,
            generated_at=moment,
            cli_version=cli_version,
            inventories=inventories,
        )
        from teatree.provisioning.skills_receipt import (  # noqa: PLC0415 — codec imports client types
            write_inventory_receipt,
        )

        write_inventory_receipt(path, receipt)
        return receipt


@contextmanager
def _inventory_refresh_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)


def read_inventory_receipt(path: Path) -> SkillsInventoryReceipt:
    from teatree.provisioning.skills_receipt import (  # noqa: PLC0415 — breaks codec↔client cycle
        read_inventory_receipt as read_receipt,
    )

    return read_receipt(path)
