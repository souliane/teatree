"""Content keys and a bounded ledger of clean, single-trial eval passes."""

import dataclasses
import hashlib
import importlib.metadata
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from teatree.eval.agent_prompt import (
    SKILL_CATALOG_FIXTURE_RELATIVE_PATH,
    resolve_agent_path,
    scenario_skill_catalog_roots,
    scenario_system_prompt,
)
from teatree.eval.artifact_redaction import write_artifact
from teatree.eval.models import DEFAULT_JUDGE_BUDGET, CachedPass, EvalSpec
from teatree.eval.production_hooks import teatree_root
from teatree.eval.toolset import scenario_exposes_subagent_spawn

VERDICT_TTL = timedelta(days=30)
_LOG = logging.getLogger(__name__)
_SCHEMA = 2
_BACKEND_PACKAGES = {
    "api": ("claude-agent-sdk",),
    "anthropic_api": ("anthropic", "pydantic-ai-slim"),
    "pydantic_ai": ("pydantic-ai-slim",),
}


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


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        raise FileNotFoundError(root)
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _optional_tree_digest(root: Path) -> str:
    return _tree_digest(root) if root.is_dir() else "missing"


def _fixture_digest(spec: EvalSpec) -> list[str]:
    if not spec.available_skills:
        return []
    core_root = teatree_root().resolve()
    catalog = core_root.joinpath(*SKILL_CATALOG_FIXTURE_RELATIVE_PATH)
    digests = [_optional_tree_digest(catalog / "skills" / name) for name in spec.available_skills]
    digests.append(_optional_tree_digest(catalog / ".claude-plugin"))
    for root in scenario_skill_catalog_roots(spec):
        digests.append(_tree_digest(root / ".claude-plugin"))
        digests.extend(_optional_tree_digest(root / "skills" / name) for name in spec.available_skills)
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
    return SharedVerdictInputs(
        harness=_tree_digest(root / "src" / "teatree"),
        hooks=_tree_digest(root / "hooks") if any(spec.production_hooks for spec in specs) else "",
        plugin=([_tree_digest(root / part) for part in ("skills", "agents", ".claude-plugin")] if needs_plugin else []),
        packages={package: importlib.metadata.version(package) for package in _BACKEND_PACKAGES[config.backend]},
    )


def verdict_key(spec: EvalSpec, config: VerdictRunConfig, *, shared_inputs: SharedVerdictInputs | None = None) -> str:
    spec_data = dataclasses.asdict(spec)
    spec_data.pop("source_path")
    spec_data["matchers"] = [_typed_data(matcher) for matcher in spec.matchers]
    shared = shared_inputs if shared_inputs is not None else _shared_inputs([spec], config)
    inputs = {
        "schema": _SCHEMA,
        "spec": spec_data,
        "system_prompt": scenario_system_prompt(spec),
        "fixtures": _fixture_digest(spec),
        "local_stubs": {
            name: hashlib.sha256((spec.source_path.parent / "cli_stubs" / f"{name}.sh").read_bytes()).hexdigest()
            for name in spec.cli_stubs
            if re.fullmatch(r"[a-z][a-z0-9_-]*@[a-z][a-z0-9_-]*", name)
        },
        "agent_skill_dir": (
            _tree_digest(resolve_agent_path(spec.agent_path, spec.source_path.parent).parent)
            if spec.fixture == "agent_skill_dir"
            else ""
        ),
        "harness": shared.harness,
        "hooks": shared.hooks if spec.production_hooks else "",
        "plugin": shared.plugin if spec.production_hooks or scenario_exposes_subagent_spawn(spec) else [],
        "packages": shared.packages,
        "run": dataclasses.asdict(config),
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


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

    def record(self, summaries: list[dict[str, Any]], *, run_url: str, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self.entries = {key: value for key, value in self.entries.items() if self.lookup(key, now=now)}
        for summary in summaries:
            if summary.get("trial_count") != 1:
                continue
            for row in summary.get("scenarios", []):
                key = row.get("content_key")
                if (
                    row.get("outcome") == "PASS"
                    and row.get("cache_eligible") is True
                    and isinstance(key, str)
                    and re.fullmatch(r"[0-9a-f]{64}", key)
                ):
                    self.entries[key] = {
                        "name": row["name"],
                        "lane": row["lane"],
                        "run_url": run_url,
                        "recorded_at": now.isoformat(),
                        "head_sha": summary.get("head_sha", ""),
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
    except (OSError, ValueError, KeyError, importlib.metadata.PackageNotFoundError) as exc:
        _LOG.warning("verdict cache: cannot key partition: %s; running fresh", exc)
        return list(specs), [], {}
    for spec in specs:
        try:
            key = verdict_key(spec, config, shared_inputs=shared)
        except (OSError, ValueError, KeyError, importlib.metadata.PackageNotFoundError) as exc:
            _LOG.warning("verdict cache: cannot key %s: %s; running fresh", spec.name, exc)
            fresh.append(spec)
            continue
        keys[spec.name] = key
        hit = ledger.lookup(key)
        if hit is None or hit.name != spec.name or hit.lane != spec.lane:
            fresh.append(spec)
        else:
            cached.append(hit)
    return fresh, cached, keys
