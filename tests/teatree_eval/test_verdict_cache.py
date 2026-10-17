import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest

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
                    {"name": "flaky", "content_key": "b" * 64, "outcome": "FLAKY"},
                    {"name": "fail", "content_key": "c" * 64, "outcome": "BEHAVIOR_FAIL"},
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


def test_key_tracks_cli_and_harness_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.eval.verdict_cache.teatree_root", lambda: tmp_path)
    cli = tmp_path / "src" / "teatree" / "cli"
    cli.mkdir(parents=True)
    (cli / "app.py").write_text("version 1")
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend="api", effort="high", max_turns=None, max_budget_usd=2, judge=False)
    original = verdict_key(spec, config)
    (cli / "app.py").write_text("version 2")
    assert verdict_key(spec, config) != original
    monkeypatch.setattr("teatree.eval.verdict_cache.importlib.metadata.version", lambda _: "other")
    assert verdict_key(spec, config) != original


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


@pytest.mark.parametrize(
    ("backend", "packages"),
    [
        ("pydantic_ai", {"pydantic-ai-slim"}),
        ("anthropic_api", {"anthropic", "pydantic-ai-slim"}),
    ],
)
def test_key_tracks_installed_backend_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, packages: set[str]
) -> None:
    seen: set[str] = set()

    def version(package: str) -> str:
        seen.add(package)
        if package == "pydantic-ai":
            raise LookupError(package)
        return "1"

    monkeypatch.setattr("teatree.eval.verdict_cache.importlib.metadata.version", version)
    spec = _spec(tmp_path)
    config = VerdictRunConfig(backend=backend, effort="high", max_turns=None, max_budget_usd=2, judge=False)
    assert len(verdict_key(spec, config)) == 64
    assert seen == packages


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
