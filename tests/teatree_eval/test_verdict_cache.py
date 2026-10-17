import dataclasses
import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest
from django.test import TestCase

from teatree.core.models import ConfigSetting
from teatree.eval import verdict_cache
from teatree.eval.models import (
    AnyOf,
    AssistantTextMatcher,
    EvalSpec,
    ExpectItem,
    FinalStateMatcher,
    JudgeSpec,
    Matcher,
    PlanBeforeToolMatcher,
    SuccessfulToolCallMatcher,
    ToolCallCountMatcher,
)
from teatree.eval.verdict_cache import VerdictLedger, VerdictRunConfig, partition, verdict_key
from tests._git_repo import make_git_repo, run_git


def _spec(tmp_path: Path) -> EvalSpec:
    agent = tmp_path / "SKILL.md"
    agent.write_text("## Graded\noriginal\n\n## Other\nunchanged\n", encoding="utf-8")
    return EvalSpec(
        name="one",
        scenario="case",
        agent_path=str(agent),
        agent_sections=("Graded",),
        prompt="act",
        matchers=(),
        source_path=tmp_path / "cases.yaml",
        model="claude-sonnet-4-6",
    )


def test_key_tracks_graded_input_but_ignores_other_sections(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    original = verdict_key(spec, config)
    agent = Path(spec.agent_path)
    agent.write_text("## Graded\noriginal\n\n## Other\nchanged\n", encoding="utf-8")
    assert verdict_key(spec, config) == original
    agent.write_text("## Graded\nchanged\n\n## Other\nchanged\n", encoding="utf-8")
    assert verdict_key(spec, config) != original


@pytest.mark.parametrize("change", ["sibling", "comment"])
def test_key_ignores_unrelated_yaml_edits(tmp_path: Path, change: str) -> None:
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    spec.source_path.write_text("- name: one\n  prompt: act\n")
    original = verdict_key(spec, config)
    addition = "- name: two\n  prompt: changed\n" if change == "sibling" else "# unrelated comment\n"
    spec.source_path.write_text(spec.source_path.read_text() + addition)
    assert verdict_key(spec, config) == original


def test_key_tracks_overlay_local_stub(tmp_path: Path) -> None:
    spec = dataclasses.replace(_spec(tmp_path), cli_stubs=("t3@profile",))
    stub = tmp_path / "cli_stubs" / "t3@profile.sh"  # privacy-scan:allow fixture CLI stub filename
    stub.parent.mkdir()
    stub.write_text("first")
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    original = verdict_key(spec, config)
    stub.write_text("second")
    assert verdict_key(spec, config) != original


@pytest.mark.parametrize("name", ["t3@on_behalf_forbidden", "glab@linked_sources"])
def test_built_in_profile_stub_is_keyable(tmp_path: Path, name: str) -> None:
    spec = dataclasses.replace(_spec(tmp_path), cli_stubs=(name,))
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    assert len(verdict_key(spec, config)) == 64


def test_key_tracks_agent_skill_dir_references(tmp_path: Path) -> None:
    spec = dataclasses.replace(_spec(tmp_path), fixture="agent_skill_dir")
    reference = tmp_path / "references" / "rule.md"
    reference.parent.mkdir()
    reference.write_text("first")
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    original = verdict_key(spec, config)
    reference.write_text("second")
    assert verdict_key(spec, config) != original


def test_missing_harness_root_forces_fresh_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.eval.verdict_cache.teatree_root", lambda: tmp_path)
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    fresh, cached, keys = partition([spec], VerdictLedger(tmp_path / "ledger.json", {}), config)
    assert fresh == [spec]
    assert cached == []
    assert keys == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt", "different"),
        ("scenario", "different"),
        ("model", "claude-opus-4-6"),
        ("max_turns", 7),
        ("available_skills", ("extra",)),
        ("cli_stubs", ("t3",)),
        ("fixture", "git_repo"),
        ("judge", JudgeSpec(rubric="different")),
        ("matchers", (Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="new"),)),
        ("tools", ("Edit",)),
        ("lane", "under_load"),
        ("surface", "interactive"),
        ("production_hooks", True),
        ("context_preamble", "loaded history"),
        ("max_budget_usd", 3.0),
        ("watchdog_seconds", 30.0),
        ("single_action", True),
        ("tier", "frontier"),
        ("phase", "coding"),
        ("agent_sections", ("Other",)),
        ("name", "two"),
    ],
)
def test_key_tracks_scenario_fields(tmp_path: Path, field: str, value: object) -> None:
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    assert verdict_key(dataclasses.replace(spec, **{field: value}), config) != verdict_key(spec, config)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend", "anthropic_api"),
        ("effort", "low"),
        ("max_turns", 8),
        ("max_budget_usd", 3),
        ("judge", True),
        ("judge_budget", 5),
    ],
)
def test_key_tracks_run_settings(tmp_path: Path, field: str, value: object) -> None:
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    assert verdict_key(spec, dataclasses.replace(config, **{field: value})) != verdict_key(spec, config)


def test_key_distinguishes_matcher_subjects_with_identical_fields(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    assistant = dataclasses.replace(spec, matchers=(AssistantTextMatcher(operator="contains", value="done"),))
    final = dataclasses.replace(spec, matchers=(FinalStateMatcher(operator="contains", value="done"),))
    assert dataclasses.asdict(assistant)["matchers"] == dataclasses.asdict(final)["matchers"]
    assert verdict_key(assistant, config) != verdict_key(final, config)


def test_key_tracks_every_matcher_kind(tmp_path: Path) -> None:
    matcher = Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="done")
    samples = (
        matcher,
        AnyOf((matcher,)),
        AssistantTextMatcher(operator="contains", value="done"),
        FinalStateMatcher(operator="contains", value="done"),
        PlanBeforeToolMatcher(governed_tools=("Bash",), patterns=("plan",)),
        ToolCallCountMatcher(tool="Bash", arg_path="command", pattern="done", equals=1, round_files=()),
        SuccessfulToolCallMatcher(
            tool="Bash",
            arg_path="command",
            operator="contains",
            value="done",
            result_operator="contains",
            result_value="ok",
            before_tool="Edit",
            before_arg_path="path",
            before_operator="contains",
            before_value="file",
        ),
    )
    assert {type(item) for item in samples} == set(get_args(ExpectItem))
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    baseline = verdict_key(spec, config)
    assert all(verdict_key(dataclasses.replace(spec, matchers=(item,)), config) != baseline for item in samples)


def test_key_tracks_all_spec_fields_except_nonbehavioral_source_location(tmp_path: Path) -> None:
    covered = {
        "name",
        "scenario",
        "agent_path",
        "prompt",
        "matchers",
        "model",
        "tier",
        "phase",
        "max_turns",
        "tools",
        "available_skills",
        "fixture",
        "cli_stubs",
        "single_action",
        "production_hooks",
        "judge",
        "agent_sections",
        "lane",
        "surface",
        "context_preamble",
        "max_budget_usd",
        "watchdog_seconds",
    }
    assert {field.name for field in dataclasses.fields(EvalSpec)} - {"source_path"} == covered
    spec = _spec(tmp_path)
    alternate = tmp_path / "alternate.md"
    alternate.write_text("## Graded\ndifferent prompt\n")
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    assert verdict_key(dataclasses.replace(spec, agent_path=str(alternate)), config) != verdict_key(spec, config)


def test_ledger_records_only_clean_passes_and_expires(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    now = datetime(2026, 10, 16, tzinfo=UTC)
    ledger = VerdictLedger.load(path)
    ledger.record(
        [
            {
                "head_sha": "a",
                "trial_count": 1,
                "scenarios": [
                    {
                        "name": "pass",
                        "lane": "clean_room",
                        "content_key": "a" * 64,
                        "outcome": "PASS",
                        "cache_eligible": True,
                    },
                    {
                        "name": "flaky",
                        "lane": "clean_room",
                        "content_key": "b" * 64,
                        "outcome": "FLAKY",
                        "cache_eligible": True,
                    },
                    {
                        "name": "fail",
                        "lane": "clean_room",
                        "content_key": "c" * 64,
                        "outcome": "BEHAVIOR_FAIL",
                        "cache_eligible": True,
                    },
                    {"name": "keyless", "outcome": "PASS"},
                    {
                        "name": "unmetered",
                        "lane": "clean_room",
                        "content_key": "d" * 64,
                        "outcome": "PASS",
                        "cache_eligible": False,
                    },
                ],
            }
        ],
        run_url="local",
        now=now,
    )
    ledger.write()
    assert list(json.loads(path.read_text())["entries"]) == ["a" * 64]
    assert VerdictLedger.load(path).lookup("a" * 64, now=now + timedelta(days=29)) is not None
    assert VerdictLedger.load(path).lookup("a" * 64, now=now + timedelta(days=31)) is None
    assert VerdictLedger.load(path).lookup("a" * 64, now=now - timedelta(seconds=1)) is None


def test_ledger_ignores_incomplete_identity_even_when_marked_eligible(tmp_path: Path) -> None:
    ledger = VerdictLedger.load(tmp_path / "ledger.json")
    ledger.record(
        [{"trial_count": 1, "scenarios": [{"content_key": "a" * 64, "outcome": "PASS", "cache_eligible": True}]}],
        run_url="local",
    )
    assert ledger.entries == {}


@pytest.mark.parametrize("scenarios", [None, "invalid", [None, 42, "invalid"]])
def test_ledger_ignores_malformed_scenarios(tmp_path: Path, scenarios: object) -> None:
    ledger = VerdictLedger.load(tmp_path / "ledger.json")
    ledger.record([{"trial_count": 1, "scenarios": scenarios}], run_url="local")
    assert ledger.entries == {}


def test_ledger_records_valid_row_after_malformed_row(tmp_path: Path) -> None:
    ledger = VerdictLedger.load(tmp_path / "ledger.json")
    ledger.record(
        [
            {
                "trial_count": 1,
                "scenarios": [
                    None,
                    {
                        "name": "pass",
                        "lane": "clean_room",
                        "content_key": "a" * 64,
                        "outcome": "PASS",
                        "cache_eligible": True,
                    },
                ],
            }
        ],
        run_url="local",
    )
    assert ledger.lookup("a" * 64) is not None


def test_ledger_write_redacts_metadata_before_it_reaches_disk(tmp_path: Path) -> None:
    token = "github_pat_" + "a" * 20
    ledger = VerdictLedger(tmp_path / "ledger.json", {"key": {"run_url": token}})

    ledger.write()

    assert json.loads(ledger.path.read_text(encoding="utf-8"))["entries"]["key"]["run_url"] == "[REDACTED]"


def test_merge_keeps_newer_valid_verdict_per_key(tmp_path: Path) -> None:
    now = datetime(2026, 10, 16, tzinfo=UTC)
    key = "a" * 64
    old = VerdictLedger(
        tmp_path / "old.json",
        {
            key: {
                "name": "one",
                "lane": "clean_room",
                "run_url": "old",
                "recorded_at": (now - timedelta(days=2)).isoformat(),
                "head_sha": "old",
            },
        },
    )
    new = VerdictLedger(
        tmp_path / "new.json",
        {
            key: {
                "name": "one",
                "lane": "clean_room",
                "run_url": "new",
                "recorded_at": now.isoformat(),
                "head_sha": "new",
            },
            "b" * 64: {
                "name": "two",
                "lane": "clean_room",
                "run_url": "expired",
                "recorded_at": (now - timedelta(days=31)).isoformat(),
                "head_sha": "old",
            },
        },
    )
    old.merge(new, now=now)
    assert old.lookup(key, now=now).run_url == "new"
    assert old.lookup("b" * 64, now=now) is None
    new.merge(old, now=now)
    assert new.lookup(key, now=now).run_url == "new"


def test_key_tracks_only_declared_fixture_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.eval.verdict_cache.teatree_root", lambda: tmp_path)
    (tmp_path / "src" / "teatree").mkdir(parents=True)
    catalog = tmp_path / "evals" / "fixtures" / "skill_catalog" / "skills"
    for name in ("review", "other"):
        skill = catalog / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(name)
    spec = dataclasses.replace(_spec(tmp_path), available_skills=("review",))
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    original = verdict_key(spec, config)
    (catalog / "other" / "SKILL.md").write_text("changed")
    assert verdict_key(spec, config) == original
    (catalog / "review" / "SKILL.md").write_text("changed")
    assert verdict_key(spec, config) != original


def test_key_tracks_runner_discovered_overlay_skill(tmp_path: Path) -> None:
    plugin = tmp_path / "overlay"
    manifest = plugin / ".claude-plugin" / "plugin.json"
    skill = plugin / "skills" / "review" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    skill.parent.mkdir(parents=True)
    manifest.write_text('{"name":"overlay"}')
    skill.write_text("first")
    spec = dataclasses.replace(_spec(plugin), available_skills=("review",))
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    before = verdict_key(spec, config)
    skill.write_text("second")
    assert verdict_key(spec, config) != before


@pytest.mark.parametrize(
    "relative", ["agents/helper.md", "hooks/on-start.sh", ".mcp.json", "settings.json", "src/overlay/hook_logic.py"]
)
def test_key_tracks_whole_overlay_plugin(tmp_path: Path, relative: str) -> None:
    plugin = tmp_path / "overlay"
    manifest = plugin / ".claude-plugin" / "plugin.json"
    skill = plugin / "skills" / "review" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    skill.parent.mkdir(parents=True)
    manifest.write_text('{"name":"overlay"}')
    skill.write_text("review")
    source = plugin / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("first")
    spec = dataclasses.replace(_spec(plugin), available_skills=("review",))
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    before = verdict_key(spec, config)
    source.write_text("second")
    assert verdict_key(spec, config) != before


@pytest.mark.parametrize(
    "relative",
    ["node_modules/cache", "teatree-upstream/README.md", "verdicts.json", ".claude/worktrees/x/SKILL.md", "CLAUDE.md"],
)
def test_key_ignores_overlay_build_and_ledger_files(tmp_path: Path, relative: str) -> None:
    plugin = tmp_path / "overlay"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name":"overlay"}')
    (plugin / "skills" / "review").mkdir(parents=True)
    (plugin / "skills" / "review" / "SKILL.md").write_text("review")
    spec = dataclasses.replace(_spec(plugin), available_skills=("review",))
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    before = verdict_key(spec, config)
    noise = plugin / relative
    noise.parent.mkdir(parents=True, exist_ok=True)
    noise.write_text("generated")
    assert verdict_key(spec, config) == before


@pytest.mark.parametrize(
    ("manifest", "relative"),
    [
        ('{"name":"overlay","commands":["./custom/review.md"]}', "custom/review.md"),
        ('{"name":"overlay","hooks":{"Stop":[{"command":"${CLAUDE_PLUGIN_ROOT}/bin/stop.sh"}]}}', "bin/stop.sh"),
    ],
)
def test_key_tracks_paths_the_plugin_manifest_declares(tmp_path: Path, manifest: str, relative: str) -> None:
    plugin = tmp_path / "overlay"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(manifest)
    (plugin / "skills" / "review").mkdir(parents=True)
    (plugin / "skills" / "review" / "SKILL.md").write_text("review")
    declared = plugin / relative
    declared.parent.mkdir(parents=True)
    declared.write_text("first")
    spec = dataclasses.replace(_spec(plugin), available_skills=("review",))
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    before = verdict_key(spec, config)
    declared.write_text("second")
    assert verdict_key(spec, config) != before


def test_tree_key_tracks_symlink_target_and_executable_mode(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    root.mkdir()
    target = tmp_path / "script.sh"
    target.write_text("first")
    (root / "script.sh").symlink_to(target)
    before = verdict_cache._tree_digest(root)
    target.write_text("second")
    changed = verdict_cache._tree_digest(root)
    assert changed != before
    target.chmod(0o755)
    assert verdict_cache._tree_digest(root) != changed


def test_tree_digest_frames_file_content(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for root in (first, second):
        (root / "a").write_bytes(b"hello")
    (first / "a").write_bytes(b"hello\0f:b:420\0world")
    (second / "b").write_bytes(b"world")
    assert verdict_cache._tree_digest(first) != verdict_cache._tree_digest(second)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("T3_EVAL_MAX_TURNS", "12"),
        ("T3_EVAL_WATCHDOG_SECONDS", "1200"),
        ("T3_EVAL_JUDGE_MAX_BUDGET_USD", "3"),
    ],
)
def test_key_tracks_effective_env_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=True)
    monkeypatch.delenv(variable, raising=False)
    before = verdict_key(spec, config)
    monkeypatch.setenv(variable, value)
    assert verdict_key(spec, config) != before


def test_subagent_key_tracks_the_entire_cloned_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "teatree").mkdir(parents=True)
    for name in ("skills", "agents", ".claude-plugin"):
        (repo / name).mkdir()
    source = repo / "outside-harness.txt"
    source.write_text("first")
    make_git_repo(repo, initial_commit=False)
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-qm", "first")
    monkeypatch.setattr(verdict_cache, "teatree_root", lambda: repo)
    monkeypatch.setattr(verdict_cache, "resolve_teatree_repo_root", lambda: repo)
    spec = dataclasses.replace(_spec(tmp_path), tools=("Agent",))
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    before = verdict_key(spec, config)
    source.write_text("second")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-qm", "second")
    assert verdict_key(spec, config) != before


@pytest.mark.parametrize("failure", ["missing", "missing_git", "timeout", "refusal"])
def test_unavailable_checkout_only_reruns_spawning_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    plain = _spec(tmp_path)
    spawning = dataclasses.replace(plain, name="spawning", tools=("Agent",))
    other_spawning = dataclasses.replace(spawning, name="other-spawning")
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    if failure == "missing_git":

        def missing_git() -> None:
            raise OSError

        monkeypatch.setattr(verdict_cache, "resolve_teatree_repo_root", missing_git)
    else:
        monkeypatch.setattr(
            verdict_cache, "resolve_teatree_repo_root", lambda: None if failure == "missing" else tmp_path
        )
    if failure == "timeout":

        def timed_out(**_kwargs: object) -> None:
            raise subprocess.TimeoutExpired(["git", "rev-parse"], 30)

        monkeypatch.setattr(verdict_cache, "run_with_status", timed_out)
    elif failure == "refusal":
        monkeypatch.setattr(
            verdict_cache, "run_with_status", lambda **_kwargs: SimpleNamespace(returncode=1, stdout="")
        )
    fresh, cached, keys = partition(
        [plain, spawning, other_spawning], VerdictLedger.load(tmp_path / "ledger.json"), config
    )
    assert fresh == [plain, spawning, other_spawning]
    assert cached == []
    assert set(keys) == {plain.name}


def _seed_cold_setting(db: Path, key: str, value: object) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute("DELETE FROM teatree_config_setting WHERE key = ?", (key,))
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)", (key, json.dumps(value))
        )
    conn.close()


class TestKeyTracksTheModelAndCapsTheBackendRuns(TestCase):
    @pytest.fixture(autouse=True)
    def _isolated_settings(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.cold_db = tmp_path / "config.sqlite3"
        monkeypatch.setenv("T3_CONFIG_DB", str(self.cold_db))
        monkeypatch.delenv("T3_OPENAI_COMPATIBLE_MODEL", raising=False)

    def _key(self, backend: str, spec: EvalSpec | None = None) -> str:
        config = VerdictRunConfig(backend=backend, effort="high", max_turns=None, max_budget_usd=2, judge=False)
        return verdict_key(spec or _spec(self.tmp_path), config)

    def _tier_spec(self) -> EvalSpec:
        return dataclasses.replace(_spec(self.tmp_path), model="", tier="frontier")

    def test_each_setting_the_backend_reads_rekeys(self) -> None:
        ConfigSetting.objects.set_value("openai_compatible_model", "vendor/model-a")
        changes = {
            "openai_compatible_model": ("vendor/model-b", ("pydantic_ai",)),
            "openai_compatible_base_url": ("https://models.example.test/v2", ("pydantic_ai",)),
            "pydantic_ai_request_limit": (41, ("pydantic_ai", "anthropic_api")),
            "pydantic_ai_max_tokens": (1234, ("pydantic_ai", "anthropic_api")),
        }
        for setting, (value, backends) in changes.items():
            with self.subTest(setting=setting):
                before = {backend: self._key(backend) for backend in backends}
                ConfigSetting.objects.set_value(setting, value)
                assert all(self._key(backend) != before[backend] for backend in backends)

    def test_key_tracks_installed_backend_packages(self) -> None:
        ConfigSetting.objects.set_value("openai_compatible_model", "vendor/model-a")
        expected = {"pydantic_ai": {"pydantic-ai-slim"}, "anthropic_api": {"anthropic", "pydantic-ai-slim"}}
        for backend, packages in expected.items():
            with self.subTest(backend=backend):
                seen: set[str] = set()

                def version(package: str, seen: set[str] = seen) -> str:
                    seen.add(package)
                    if package == "pydantic-ai":
                        raise LookupError(package)
                    return "1"

                self.monkeypatch.setattr("teatree.eval.verdict_cache.importlib.metadata.version", version)
                assert len(self._key(backend)) == 64
                assert seen == packages

    def test_each_backend_package_version_rekeys(self) -> None:
        ConfigSetting.objects.set_value("openai_compatible_model", "vendor/model-a")
        for backend, changed in (
            ("api", "claude-agent-sdk"),
            ("anthropic_api", "anthropic"),
            ("anthropic_api", "pydantic-ai-slim"),
            ("pydantic_ai", "pydantic-ai-slim"),
        ):
            with self.subTest(backend=backend, package=changed):
                self.monkeypatch.setattr("teatree.eval.verdict_cache.importlib.metadata.version", lambda _: "1")
                before = self._key(backend)
                self.monkeypatch.setattr(
                    "teatree.eval.verdict_cache.importlib.metadata.version",
                    lambda package, changed=changed: "2" if package == changed else "1",
                )
                assert self._key(backend) != before

    def test_openai_model_environment_override_rekeys(self) -> None:
        self.monkeypatch.setenv("T3_OPENAI_COMPATIBLE_MODEL", "vendor/model-a")
        before = self._key("pydantic_ai")
        self.monkeypatch.setenv("T3_OPENAI_COMPATIBLE_MODEL", "vendor/model-b")
        assert self._key("pydantic_ai") != before

    def test_tier_model_override_rekeys(self) -> None:
        for backend in ("api", "anthropic_api"):
            with self.subTest(backend=backend):
                _seed_cold_setting(self.cold_db, "agent_tier_models", {"frontier": "claude-model-a"})
                before = self._key(backend, self._tier_spec())
                _seed_cold_setting(self.cold_db, "agent_tier_models", {"frontier": "claude-model-b"})
                assert self._key(backend, self._tier_spec()) != before

    def test_pydantic_ai_tier_model_override_rekeys(self) -> None:
        _seed_cold_setting(self.cold_db, "agent_pydantic_ai_tier_models", {"frontier": "vendor/model-a"})
        before = self._key("pydantic_ai", self._tier_spec())
        _seed_cold_setting(self.cold_db, "agent_pydantic_ai_tier_models", {"frontier": "vendor/model-b"})
        assert self._key("pydantic_ai", self._tier_spec()) != before

    def test_unconfigured_pydantic_ai_model_runs_fresh(self) -> None:
        spec = self._tier_spec()
        config = VerdictRunConfig(backend="pydantic_ai", effort="high", max_turns=None, max_budget_usd=2, judge=False)
        assert partition([spec], VerdictLedger.load(self.tmp_path / "ledger.json"), config) == ([spec], [], {})


def test_key_tracks_cli_and_harness_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.eval.verdict_cache.teatree_root", lambda: tmp_path)
    cli = tmp_path / "src" / "teatree" / "cli"
    cli.mkdir(parents=True)
    (cli / "app.py").write_text("version 1")
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    original = verdict_key(spec, config)
    (cli / "app.py").write_text("version 2")
    after_cli = verdict_key(spec, config)
    assert after_cli != original
    monkeypatch.setattr("teatree.eval.verdict_cache.importlib.metadata.version", lambda _: "other")
    assert verdict_key(spec, config) != after_cli


@pytest.mark.parametrize(
    "relative", ["agents/permission_modes.py", "llm/client.py", "config/settings.py", "core/cost.py"]
)
def test_key_tracks_runner_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str) -> None:
    monkeypatch.setattr("teatree.eval.verdict_cache.teatree_root", lambda: tmp_path)
    source = tmp_path / "src" / "teatree" / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("first")
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    before = verdict_key(spec, config)
    source.write_text("second")
    assert verdict_key(spec, config) != before


@pytest.mark.parametrize("relative", ["skills/code/SKILL.md", "agents/reviewer.md", ".claude-plugin/plugin.json"])
def test_hooked_key_tracks_shipped_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str) -> None:
    monkeypatch.setattr("teatree.eval.verdict_cache.teatree_root", lambda: tmp_path)
    (tmp_path / "src" / "teatree").mkdir(parents=True)
    for part in ("hooks", "skills", "agents", ".claude-plugin"):
        (tmp_path / part).mkdir()
    source = tmp_path / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("first")
    spec = dataclasses.replace(_spec(tmp_path), production_hooks=True)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    before = verdict_key(spec, config)
    source.write_text("second")
    assert verdict_key(spec, config) != before


def test_graded_section_change_invalidates_only_its_scenario(tmp_path: Path) -> None:
    spec_a = _spec(tmp_path)
    spec_b = dataclasses.replace(spec_a, name="two", agent_sections=("Other",))
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    prior = {spec.name: verdict_key(spec, config) for spec in (spec_a, spec_b)}
    Path(spec_a.agent_path).write_text("## Graded\nchanged\n\n## Other\nunchanged\n", encoding="utf-8")
    assert verdict_key(spec_a, config) != prior[spec_a.name]
    assert verdict_key(spec_b, config) == prior[spec_b.name]


def test_partition_reruns_only_the_changed_graded_section(tmp_path: Path) -> None:
    spec_a = _spec(tmp_path)
    spec_b = dataclasses.replace(spec_a, name="two", agent_sections=("Other",))
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    ledger = VerdictLedger.load(tmp_path / "verdicts.json")
    ledger.record(
        [
            {
                "head_sha": "abc",
                "trial_count": 1,
                "scenarios": [
                    {
                        "name": spec.name,
                        "lane": spec.lane,
                        "content_key": verdict_key(spec, config),
                        "outcome": "PASS",
                        "cache_eligible": True,
                    }
                    for spec in (spec_a, spec_b)
                ],
            }
        ],
        run_url="https://example.test/run/1",
    )
    Path(spec_a.agent_path).write_text("## Graded\nchanged\n\n## Other\nunchanged\n", encoding="utf-8")
    fresh, cached, _keys = partition([spec_a, spec_b], ledger, config)
    assert [spec.name for spec in fresh] == ["one"]
    assert [hit.name for hit in cached] == ["two"]
    assert cached[0].model == spec_b.model


def test_partition_keeps_scenario_grading_another_file_cached(tmp_path: Path) -> None:
    changed = _spec(tmp_path)
    other_file = tmp_path / "OTHER.md"
    other_file.write_text("## Graded\noriginal\n", encoding="utf-8")
    unchanged = dataclasses.replace(changed, name="other", agent_path=str(other_file))
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    ledger = VerdictLedger.load(tmp_path / "verdicts.json")
    ledger.record(
        [
            {
                "head_sha": "abc",
                "trial_count": 1,
                "scenarios": [
                    {
                        "name": spec.name,
                        "lane": spec.lane,
                        "content_key": verdict_key(spec, config),
                        "outcome": "PASS",
                        "cache_eligible": True,
                    }
                    for spec in (changed, unchanged)
                ],
            }
        ],
        run_url="https://example.test/run/1",
    )
    Path(changed.agent_path).write_text("## Graded\nchanged\n", encoding="utf-8")
    fresh, cached, _keys = partition([changed, unchanged], ledger, config)
    assert [spec.name for spec in fresh] == ["one"]
    assert [hit.name for hit in cached] == ["other"]


def test_partition_hashes_shared_code_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    original = verdict_cache._tree_digest
    code_root = verdict_cache.teatree_root() / "src" / "teatree"
    calls = 0

    def counted(root: Path) -> str:
        nonlocal calls
        if root == code_root:
            calls += 1
        return original(root)

    monkeypatch.setattr(verdict_cache, "_tree_digest", counted)
    partition([spec, dataclasses.replace(spec, name="two")], VerdictLedger.load(tmp_path / "ledger.json"), config)
    assert calls == 1


def test_partition_hashes_shared_overlay_plugin_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = tmp_path / "overlay"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name":"overlay"}')
    (plugin / "skills" / "review").mkdir(parents=True)
    (plugin / "skills" / "review" / "SKILL.md").write_text("review")
    spec = dataclasses.replace(_spec(plugin), available_skills=("review",))
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    original = verdict_cache._plugin_digest
    calls = 0

    def counted(root: Path) -> str:
        nonlocal calls
        if root == plugin:
            calls += 1
        return original(root)

    monkeypatch.setattr(verdict_cache, "_plugin_digest", counted)
    partition([spec, dataclasses.replace(spec, name="two")], VerdictLedger.load(tmp_path / "ledger.json"), config)
    assert calls == 1
