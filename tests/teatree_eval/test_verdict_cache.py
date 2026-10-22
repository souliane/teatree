import dataclasses
import json
import shutil
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
from teatree.eval.discovery import discover_specs
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


@pytest.mark.parametrize(
    ("kind", "relative", "manifest"),
    [
        ("graded_section", "", None),
        ("cli_stub", "", None),
        ("agent_skill_dir", "", None),
        ("fixture_skill", "", None),
        *[
            ("overlay_plugin", path, None)
            for path in (
                "skills/review/SKILL.md",
                "agents/helper.md",
                "hooks/on-start.sh",
                ".mcp.json",
                "settings.json",
                "src/overlay/hook_logic.py",
            )
        ],
        ("overlay_plugin", "custom/review.md", '{"name":"overlay","commands":["./custom/review.md"]}'),
        (
            "overlay_plugin",
            "bin/stop.sh",
            '{"name":"overlay","hooks":{"Stop":[{"command":"${CLAUDE_PLUGIN_ROOT}/bin/stop.sh"}]} }',
        ),
        *[
            ("shipped_plugin", path, None)
            for path in (
                "skills/code/SKILL.md",
                "agents/reviewer.md",
                ".claude-plugin/plugin.json",
                "hooks/on-start.sh",
            )
        ],
    ],
)
def test_key_tracks_graded_sections_and_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, relative: str, manifest: str | None
) -> None:
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    catalog = tmp_path / "evals" / "fixtures" / "skill_catalog" / "skills"
    if kind == "graded_section":
        target = Path(spec.agent_path)
        before = verdict_key(spec, config)
        target.write_text("## Graded\noriginal\n\n## Other\nchanged\n", encoding="utf-8")
        assert verdict_key(spec, config) == before
        target.write_text("## Graded\nchanged\n\n## Other\nchanged\n", encoding="utf-8")
        assert verdict_key(spec, config) != before
        return
    if kind == "cli_stub":
        spec = dataclasses.replace(spec, cli_stubs=("t3@profile",))
        target = tmp_path / "cli_stubs" / "t3@profile.sh"  # privacy-scan:allow fixture CLI stub filename
    elif kind == "agent_skill_dir":
        spec = dataclasses.replace(spec, fixture="agent_skill_dir")
        target = tmp_path / "references" / "rule.md"
    elif kind == "fixture_skill":
        monkeypatch.setattr(verdict_cache, "teatree_root", lambda: tmp_path)
        (tmp_path / "src" / "teatree").mkdir(parents=True)
        (catalog / "other").mkdir(parents=True)
        (catalog / "other" / "SKILL.md").write_text("other")
        spec = dataclasses.replace(spec, available_skills=("review",))
        target = catalog / "review" / "SKILL.md"
    elif kind == "overlay_plugin":
        plugin = tmp_path / "overlay"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(manifest or '{"name":"overlay"}')
        (plugin / "skills" / "review").mkdir(parents=True)
        (plugin / "skills" / "review" / "SKILL.md").write_text("review")
        spec = dataclasses.replace(_spec(plugin), available_skills=("review",))
        target = plugin / relative
    else:
        monkeypatch.setattr(verdict_cache, "teatree_root", lambda: tmp_path)
        for part in ("src/teatree", "hooks", "skills", "agents", ".claude-plugin"):
            (tmp_path / part).mkdir(parents=True, exist_ok=True)
        spec = dataclasses.replace(spec, production_hooks=True)
        target = tmp_path / relative

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("first")
    before = verdict_key(spec, config)
    if kind == "fixture_skill":
        (catalog / "other" / "SKILL.md").write_text("changed")
        assert verdict_key(spec, config) == before
    target.write_text("second")
    assert verdict_key(spec, config) != before


@pytest.mark.parametrize("change", ["sibling", "comment"])
def test_key_ignores_unrelated_yaml_edits(tmp_path: Path, change: str) -> None:
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    spec.source_path.write_text("- name: one\n  prompt: act\n")
    original = verdict_key(spec, config)
    addition = "- name: two\n  prompt: changed\n" if change == "sibling" else "# unrelated comment\n"
    spec.source_path.write_text(spec.source_path.read_text() + addition)
    assert verdict_key(spec, config) == original


@pytest.mark.parametrize("name", ["t3@on_behalf_forbidden", "glab@linked_sources"])
def test_built_in_profile_stub_is_keyable(tmp_path: Path, name: str) -> None:
    spec = dataclasses.replace(_spec(tmp_path), cli_stubs=(name,))
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    assert len(verdict_key(spec, config)) == 64


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
        ("agent_path", "alternate.md"),
        ("name", "two"),
    ],
)
def test_key_tracks_scenario_fields(tmp_path: Path, field: str, value: object) -> None:
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    if field == "agent_path":
        alternate = tmp_path / str(value)
        alternate.write_text("## Graded\ndifferent prompt\n")
        value = str(alternate)
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


def test_key_tracks_all_spec_fields_except_nonbehavioral_source_location() -> None:
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


class TestVerdictKeySettings(TestCase):
    @pytest.fixture(autouse=True)
    def _isolated_settings(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_key_tracks_model_and_caps(self) -> None:
        cases = [
            ("setting", "pydantic_ai", "openai_compatible_model", "vendor/model-b"),
            ("setting", "pydantic_ai", "openai_compatible_base_url", "https://models.example.test/v2"),
            ("setting", "pydantic_ai", "pydantic_ai_request_limit", 41),
            ("setting", "anthropic_api", "pydantic_ai_request_limit", 41),
            ("setting", "pydantic_ai", "pydantic_ai_max_tokens", 1234),
            ("setting", "anthropic_api", "pydantic_ai_max_tokens", 1234),
            ("env_model", "pydantic_ai", "T3_OPENAI_COMPATIBLE_MODEL", "vendor/model-b"),
            ("tier_model", "api", "agent_tier_models", "claude-model-b"),
            ("tier_model", "anthropic_api", "agent_tier_models", "claude-model-b"),
            ("tier_model", "pydantic_ai", "agent_pydantic_ai_tier_models", "vendor/model-b"),
            ("env_cap", "api", "T3_EVAL_" + "MAX_TURNS", "12"),
            ("env_cap", "api", "T3_EVAL_WATCHDOG_SECONDS", "1200"),
            ("env_cap", "api", "T3_EVAL_JUDGE_MAX_BUDGET_USD", "3"),
        ]
        baselines = {
            "openai_compatible_model": "vendor/model-a",
            "openai_compatible_base_url": "https://models.example.test/v1",
            "pydantic_ai_request_limit": 40,
            "pydantic_ai_max_tokens": 1233,
        }
        for index, (kind, backend, name, value) in enumerate(cases):
            with self.subTest(kind=kind, backend=backend, name=name), self.monkeypatch.context() as patcher:
                cold_db = self.tmp_path / f"config-{index}.sqlite3"
                patcher.setenv("T3_CONFIG_DB", str(cold_db))
                patcher.delenv("T3_OPENAI_COMPATIBLE_MODEL", raising=False)
                ConfigSetting.objects.set_value("openai_compatible_model", "vendor/model-a")
                spec = _spec(self.tmp_path)
                if kind == "setting":
                    ConfigSetting.objects.set_value(name, baselines[name])
                elif kind == "tier_model":
                    spec = dataclasses.replace(spec, model="", tier="frontier")
                    if backend == "pydantic_ai":
                        ConfigSetting.objects.set_value("openai_compatible_model", "")
                    initial = "vendor/model-a" if backend == "pydantic_ai" else "claude-model-a"
                    _seed_cold_setting(cold_db, name, {"frontier": initial})
                elif kind == "env_model":
                    patcher.setenv(name, "vendor/model-a")
                else:
                    patcher.delenv(name, raising=False)
                config = VerdictRunConfig(
                    backend=backend,
                    effort="high",
                    max_turns=None,
                    max_budget_usd=2,
                    judge=kind == "env_cap" and name == "T3_EVAL_JUDGE_MAX_BUDGET_USD",
                )
                before = verdict_key(spec, config)
                if kind == "setting":
                    ConfigSetting.objects.set_value(name, value)
                elif kind == "tier_model":
                    _seed_cold_setting(cold_db, name, {"frontier": value})
                else:
                    patcher.setenv(name, str(value))
                assert verdict_key(spec, config) != before

    def test_unconfigured_pydantic_ai_model_runs_fresh(self) -> None:
        with self.monkeypatch.context() as patcher:
            patcher.setenv("T3_CONFIG_DB", str(self.tmp_path / "unconfigured.sqlite3"))
            patcher.delenv("T3_OPENAI_COMPATIBLE_MODEL", raising=False)
            spec = dataclasses.replace(_spec(self.tmp_path), model="", tier="frontier")
            config = VerdictRunConfig(
                backend="pydantic_ai", effort="high", max_turns=None, max_budget_usd=2, judge=False
            )
            assert partition([spec], VerdictLedger.load(self.tmp_path / "ledger.json"), config) == ([spec], [], {})

    def test_key_tracks_harness_and_package_versions(self) -> None:
        cases = [
            *[
                ("harness", "api", None, relative)
                for relative in (
                    "cli/app.py",
                    "agents/permission_modes.py",
                    "llm/anthropic_limits.py",
                    "config/settings.py",
                    "core/modelkit/phases.py",
                )
            ],
            ("package", "api", "claude-agent-sdk", None),
            ("package", "anthropic_api", "anthropic", None),
            ("package", "anthropic_api", "pydantic-ai-slim", None),
            ("package", "pydantic_ai", "pydantic-ai-slim", None),
        ]
        for kind, backend, package, relative in cases:
            with (
                self.subTest(kind=kind, backend=backend, package=package, relative=relative),
                self.monkeypatch.context() as patcher,
            ):
                patcher.setenv("T3_CONFIG_DB", str(self.tmp_path / "versions.sqlite3"))
                ConfigSetting.objects.set_value("openai_compatible_model", "vendor/model-a")
                spec = _spec(self.tmp_path)
                config = VerdictRunConfig(backend=backend, effort="high", max_turns=None, max_budget_usd=2, judge=False)
                source_file = self.tmp_path / "src" / "teatree" / relative if relative else None
                if source_file is not None:
                    patcher.setattr(verdict_cache, "teatree_root", lambda: self.tmp_path)
                    source_file.parent.mkdir(parents=True, exist_ok=True)
                    source_file.write_text("version 1")
                else:
                    patcher.setattr(verdict_cache.importlib.metadata, "version", lambda _: "1")
                before = verdict_key(spec, config)
                if source_file is not None:
                    source_file.write_text("version 2")
                else:
                    patcher.setattr(
                        verdict_cache.importlib.metadata,
                        "version",
                        lambda candidate, package=package: "2" if candidate == package else "1",
                    )
                assert verdict_key(spec, config) != before


def test_real_scenario_catalog_keys_are_stable(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is required to key sub-agent scenarios")
    specs = discover_specs()
    assert specs
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2.0, judge=False)
    ledger = VerdictLedger(tmp_path / "verdicts.json", {})
    first = partition(specs, ledger, config)[2]
    second = partition(specs, ledger, config)[2]
    assert set(first) == {spec.name for spec in specs}
    assert first == second


@pytest.mark.parametrize("other_source", ["other-section", "sibling-file"])
def test_partition_reruns_only_the_changed_graded_section(tmp_path: Path, other_source: str) -> None:
    spec_a = _spec(tmp_path)
    if other_source == "sibling-file":
        other_file = tmp_path / "OTHER.md"
        other_file.write_text("## Graded\noriginal\n", encoding="utf-8")
        spec_b = dataclasses.replace(spec_a, name="two", agent_path=str(other_file))
    else:
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
