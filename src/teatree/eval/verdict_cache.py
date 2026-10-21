"""Content keys and a bounded ledger of clean, single-trial eval passes."""

import dataclasses
import hashlib
import importlib.metadata
import json
import logging
import re
import stat
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from teatree.agents.model_tiering import UnconfiguredOpenAICompatibleModelError
from teatree.config import get_effective_settings
from teatree.eval.agent_prompt import (
    SKILL_CATALOG_FIXTURE_RELATIVE_PATH,
    resolve_agent_path,
    scenario_skill_catalog_roots,
    scenario_system_prompt,
)
from teatree.eval.artifact_redaction import write_artifact
from teatree.eval.cli_stub_fixture import stub_body
from teatree.eval.ephemeral_checkout import resolve_teatree_repo_root
from teatree.eval.judge import resolve_judge_budget_usd
from teatree.eval.model_resolution import resolve_pydantic_ai_spec_model, resolve_spec_model
from teatree.eval.models import DEFAULT_JUDGE_BUDGET, CachedPass, EvalSpec
from teatree.eval.production_hooks import teatree_root
from teatree.eval.resource_caps import resolve_max_turns_override, resolve_watchdog_seconds
from teatree.eval.toolset import scenario_exposes_subagent_spawn
from teatree.eval.triage import EvalSummaryRecord
from teatree.utils.git_run import git_env_without_overrides, run_with_status
from teatree.utils.run import TimeoutExpired

VERDICT_TTL = timedelta(days=30)
_LOG = logging.getLogger(__name__)
_SCHEMA = 2
_BACKEND_PACKAGES = {
    "api": ("claude-agent-sdk",),
    "anthropic_api": ("anthropic", "pydantic-ai-slim"),
    "pydantic_ai": ("pydantic-ai-slim",),
}
_BACKEND_SETTING_NAMES = (
    "openai_compatible_model",
    "openai_compatible_base_url",
    "pydantic_ai_request_limit",
    "pydantic_ai_max_tokens",
)
_PLUGIN_PATHS = (
    ".claude-plugin",
    ".mcp.json",
    ".lsp.json",
    "settings.json",
    "agents",
    "commands",
    "hooks",
    "output-styles",
    "scripts",
    "skills",
    "src",
)
_PLUGIN_MANIFESTS = (".claude-plugin/plugin.json", "hooks/hooks.json", ".mcp.json", ".lsp.json")
_PLUGIN_REFERENCE = re.compile(r"(?:\$\{?CLAUDE_PLUGIN_ROOT\}?|\"\.)/([^/\\\"'\s]+)")
_KEY_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    KeyError,
    importlib.metadata.PackageNotFoundError,
    UnconfiguredOpenAICompatibleModelError,
)


@dataclasses.dataclass(frozen=True)
class VerdictRunConfig:
    backend: str
    effort: str | None
    max_turns: int | None
    max_budget_usd: float
    judge: bool
    judge_budget: int = DEFAULT_JUDGE_BUDGET


@dataclasses.dataclass(frozen=True)
class SharedVerdictInputs:
    harness: str
    hooks: str
    plugin: list[str]
    packages: dict[str, str]
    backend_settings: dict[str, str | int] = dataclasses.field(default_factory=dict)
    checkout_tree: str | None = None
    overlay_plugins: dict[Path, str] = dataclasses.field(default_factory=dict)


def _tree_children(path: Path) -> list[Path]:
    return [
        child
        for child in sorted(path.iterdir())
        if child.name not in {".git", ".venv", "__pycache__"} and child.suffix != ".pyc"
    ]


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        raise FileNotFoundError(root)

    def add(payload: bytes) -> None:
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    def visit(path: Path, ancestors: frozenset[tuple[int, int]]) -> None:
        metadata = path.stat()
        identity = (metadata.st_dev, metadata.st_ino)
        if path.is_dir():
            if identity in ancestors:
                message = f"symlink cycle in {root}"
                raise ValueError(message)
            add(f"d:{path.relative_to(root).as_posix()}:{stat.S_IMODE(metadata.st_mode)}".encode())
            for child in _tree_children(path):
                visit(child, ancestors | {identity})
        elif path.is_file():
            add(f"f:{path.relative_to(root).as_posix()}:{stat.S_IMODE(metadata.st_mode)}".encode())
            add(path.read_bytes())

    for child in _tree_children(root):
        visit(child, frozenset({(root.stat().st_dev, root.stat().st_ino)}))
    return digest.hexdigest()


def _plugin_paths(root: Path) -> list[str]:
    declared = {
        name
        for manifest in _PLUGIN_MANIFESTS
        if (root / manifest).is_file()
        for name in _PLUGIN_REFERENCE.findall((root / manifest).read_text(encoding="utf-8", errors="replace"))
    }
    return sorted({*_PLUGIN_PATHS, *declared} - {".", ".."})


def _plugin_digest(root: Path) -> str:
    if not root.is_dir():
        raise FileNotFoundError(root)
    paths = []
    for name in _plugin_paths(root):
        path = root / name
        if path.is_dir():
            content = _tree_digest(path)
        elif path.is_file():
            content = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            content = "missing"
        paths.append((name, stat.S_IMODE(path.stat().st_mode) if path.exists() else 0, content))
    return hashlib.sha256(json.dumps(paths, separators=(",", ":")).encode()).hexdigest()


def _optional_tree_digest(root: Path) -> str:
    return _tree_digest(root) if root.is_dir() else "missing"


def _fixture_digest(spec: EvalSpec, overlay_plugins: Mapping[Path, str]) -> list[str]:
    if not spec.available_skills:
        return []
    core_root = teatree_root().resolve()
    catalog = core_root.joinpath(*SKILL_CATALOG_FIXTURE_RELATIVE_PATH)
    digests = [_optional_tree_digest(catalog / "skills" / name) for name in spec.available_skills]
    digests.append(_optional_tree_digest(catalog / ".claude-plugin"))
    digests.extend(overlay_plugins.get(root) or _plugin_digest(root) for root in scenario_skill_catalog_roots(spec))
    return digests


def _typed_data(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {field.name: _typed_data(getattr(value, field.name)) for field in dataclasses.fields(value)}
        return {"__type__": f"{type(value).__module__}.{type(value).__qualname__}", **fields}
    if isinstance(value, tuple):
        return [_typed_data(item) for item in value]
    return value


def _shared_inputs(specs: list[EvalSpec], config: VerdictRunConfig) -> SharedVerdictInputs:
    root = teatree_root()
    needs_plugin = any(spec.production_hooks or scenario_exposes_subagent_spawn(spec) for spec in specs)
    overlay_roots = {plugin for spec in specs for plugin in scenario_skill_catalog_roots(spec)}
    settings = get_effective_settings() if config.backend in {"pydantic_ai", "anthropic_api"} else None
    return SharedVerdictInputs(
        harness=_tree_digest(root / "src" / "teatree"),
        hooks=_tree_digest(root / "hooks") if any(spec.production_hooks for spec in specs) else "",
        plugin=([_tree_digest(root / part) for part in ("skills", "agents", ".claude-plugin")] if needs_plugin else []),
        packages={package: importlib.metadata.version(package) for package in _BACKEND_PACKAGES[config.backend]},
        backend_settings={name: getattr(settings, name) for name in _BACKEND_SETTING_NAMES} if settings else {},
        checkout_tree=_checkout_tree() if any(scenario_exposes_subagent_spawn(spec) for spec in specs) else None,
        overlay_plugins={plugin: _plugin_digest(plugin) for plugin in sorted(overlay_roots)},
    )


def _checkout_tree() -> str | None:
    try:
        repo_root = resolve_teatree_repo_root()
        if repo_root is None:
            return None
        revision = run_with_status(
            repo=str(repo_root), args=["rev-parse", "HEAD^{tree}"], env=git_env_without_overrides(), timeout=30
        )
    except (OSError, TimeoutExpired) as exc:
        _LOG.warning("verdict cache: cannot read the sub-agent checkout tree: %s", exc)
        return None
    tree = revision.stdout.strip()
    return tree if revision.returncode == 0 and tree else None


def _run_model(spec: EvalSpec, backend: str, settings: Mapping[str, str | int]) -> str:
    resolved = resolve_spec_model(spec)
    if backend != "pydantic_ai":
        return resolved.model
    return resolve_pydantic_ai_spec_model(resolved, configured_model=str(settings["openai_compatible_model"]) or None)


def verdict_key(spec: EvalSpec, config: VerdictRunConfig, *, shared_inputs: SharedVerdictInputs | None = None) -> str:
    spec_data = dataclasses.asdict(spec)
    spec_data.pop("source_path")
    spec_data["matchers"] = [_typed_data(matcher) for matcher in spec.matchers]
    shared = shared_inputs if shared_inputs is not None else _shared_inputs([spec], config)
    spawns = scenario_exposes_subagent_spawn(spec)
    if spawns and shared.checkout_tree is None:
        message = "sub-agent checkout tree unavailable"
        raise FileNotFoundError(message)
    inputs = {
        "schema": _SCHEMA,
        "spec": spec_data,
        "system_prompt": scenario_system_prompt(spec),
        "fixtures": _fixture_digest(spec, shared.overlay_plugins),
        "cli_stubs": {
            name: hashlib.sha256(stub_body(name, spec.source_path).encode()).hexdigest() for name in spec.cli_stubs
        },
        "agent_skill_dir": (
            _tree_digest(resolve_agent_path(spec.agent_path, spec.source_path.parent).parent)
            if spec.fixture == "agent_skill_dir"
            else ""
        ),
        "harness": shared.harness,
        "hooks": shared.hooks if spec.production_hooks else "",
        "plugin": shared.plugin if spec.production_hooks or spawns else [],
        "checkout_tree": shared.checkout_tree if spawns else "",
        "packages": shared.packages,
        "backend_settings": shared.backend_settings,
        "model": _run_model(spec, config.backend, shared.backend_settings),
        "run": {
            **dataclasses.asdict(config),
            "effective_max_turns": resolve_max_turns_override(config.max_turns),
            "watchdog_seconds": spec.watchdog_seconds or resolve_watchdog_seconds(),
            "judge_budget_usd": resolve_judge_budget_usd() if config.judge else None,
        },
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclasses.dataclass
class VerdictLedger:
    path: Path
    entries: dict[str, dict[str, str]]

    @classmethod
    def load(cls, path: Path) -> "VerdictLedger":
        if not path.exists():
            return cls(path, {})
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema") == _SCHEMA and isinstance(payload.get("entries"), dict):
                return cls(path, payload["entries"])
            _LOG.warning("verdict cache: unknown ledger schema in %s; running fresh", path)
            return cls(path, {})
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            _LOG.warning("verdict cache: unreadable ledger %s: %s; running fresh", path, exc)
            return cls(path, {})

    def lookup(self, key: str, *, now: datetime | None = None) -> CachedPass | None:
        item = self.entries.get(key)
        if not isinstance(item, dict):
            return None
        now = now or datetime.now(UTC)
        try:
            recorded = datetime.fromisoformat(item["recorded_at"])
            if recorded.tzinfo is None or not timedelta() <= now - recorded <= VERDICT_TTL:
                return None
            return CachedPass(
                name=item["name"],
                lane=item["lane"],
                content_key=key,
                run_url=item["run_url"],
                recorded_at=item["recorded_at"],
                head_sha=item["head_sha"],
            )
        except (KeyError, TypeError, ValueError):
            return None

    def record(self, summaries: list[EvalSummaryRecord], *, run_url: str, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self.entries = {key: value for key, value in self.entries.items() if self.lookup(key, now=now)}
        for summary in summaries:
            if summary.get("trial_count") != 1:
                continue
            scenarios = summary.get("scenarios")
            if not isinstance(scenarios, list):
                continue
            for row in scenarios:
                if not isinstance(row, dict):
                    continue
                key = row.get("content_key")
                name = row.get("name")
                lane = row.get("lane")
                if row.get("outcome") != "PASS" or row.get("cache_eligible") is not True:
                    continue
                if not isinstance(name, str) or not name or not isinstance(lane, str) or not lane:
                    continue
                if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None:
                    continue
                head_sha = summary.get("head_sha")
                self.entries[key] = {
                    "name": name,
                    "lane": lane,
                    "run_url": run_url,
                    "recorded_at": now.isoformat(),
                    "head_sha": head_sha if isinstance(head_sha, str) else "",
                }

    def merge(self, other: "VerdictLedger", *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self.entries = {key: value for key, value in self.entries.items() if self.lookup(key, now=now)}
        for key, value in other.entries.items():
            incoming = other.lookup(key, now=now)
            current = self.lookup(key, now=now)
            if incoming is not None and (
                current is None
                or datetime.fromisoformat(incoming.recorded_at) > datetime.fromisoformat(current.recorded_at)
            ):
                self.entries[key] = value

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"schema": _SCHEMA, "entries": self.entries}, sort_keys=True, indent=2)
        write_artifact(self.path, content, atomic=True)


def partition(
    specs: list[EvalSpec], ledger: VerdictLedger, config: VerdictRunConfig
) -> tuple[list[EvalSpec], list[CachedPass], dict[str, str]]:
    fresh: list[EvalSpec] = []
    cached: list[CachedPass] = []
    keys: dict[str, str] = {}
    try:
        shared = _shared_inputs(specs, config)
    except _KEY_ERRORS as exc:
        _LOG.warning("verdict cache: cannot key partition: %s; running fresh", exc)
        return list(specs), [], {}
    for spec in specs:
        try:
            key = verdict_key(spec, config, shared_inputs=shared)
        except _KEY_ERRORS as exc:
            _LOG.warning("verdict cache: cannot key %s: %s; running fresh", spec.name, exc)
            fresh.append(spec)
            continue
        keys[spec.name] = key
        hit = ledger.lookup(key)
        if hit is None or hit.name != spec.name or hit.lane != spec.lane:
            fresh.append(spec)
        else:
            cached.append(dataclasses.replace(hit, model=spec.model))
    return fresh, cached, keys
