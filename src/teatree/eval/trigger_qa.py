"""Trigger-QA: deterministic skill-activation eval.

A behavioral eval (``scenarios/*.yaml``) checks what an agent *does* once a
skill is loaded. Trigger-QA checks the step before that: does the skill's
declared ``triggers.keywords`` frontmatter actually *fire* on the prompts the
skill claims to handle, and stay *quiet* on unrelated control prompts?

This is a Layer-1 (code-enforceable) eval per ``README.md`` — it runs for free
in CI, no ``claude -p`` invocation. Each skill's frontmatter is the source of
truth; the corpus of must-fire / must-not-fire prompts lives in
``trigger_qa_corpus.yaml`` beside this module so a skill author edits one file
to register expectations.
"""

import dataclasses
import json
import re
from pathlib import Path

import yaml

from teatree.trigger_parser import parse_triggers

CORPUS_PATH = Path(__file__).parent / "trigger_qa_corpus.yaml"
# ``skills/`` sits next to ``src/`` in the teatree tree; resolve it from this
# module's path so trigger-QA stays a leaf of the eval package (it must not
# reach up into ``teatree.skill_support.loading``, a higher-level module — the same
# backwards-edge rule this eval's sibling scenario gates).
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"


@dataclasses.dataclass(frozen=True)
class TriggerCheck:
    skill: str
    prompt: str
    should_fire: bool
    fired: bool

    @property
    def ok(self) -> bool:
        return self.fired == self.should_fire


@dataclasses.dataclass(frozen=True)
class TriggerQAReport:
    checks: tuple[TriggerCheck, ...]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> tuple[TriggerCheck, ...]:
        return tuple(c for c in self.checks if not c.ok)


def _keyword_patterns(skill: str, skills_dir: Path) -> list[re.Pattern[str]]:
    skill_md = skills_dir / skill / "SKILL.md"
    if not skill_md.is_file():
        return []
    triggers = parse_triggers(skill_md.read_text(encoding="utf-8"))
    if not triggers:
        return []
    patterns: list[re.Pattern[str]] = []
    for raw in triggers.get("keywords", []):
        try:
            patterns.append(re.compile(raw, re.IGNORECASE))
        except re.error:
            continue
    return patterns


def _fires(prompt: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(prompt) for p in patterns)


def load_corpus(path: Path = CORPUS_PATH) -> list[dict]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, list) else []


def render_text(report: TriggerQAReport) -> str:
    lines: list[str] = []
    for check in report.failures:
        kind = "under-trigger (expected fire, none)" if check.should_fire else "over-trigger (fired, unexpected)"
        lines.append(f"FAIL {check.skill}: {kind}\n  prompt: {check.prompt}")
    passed = len(report.checks) - len(report.failures)
    lines.append(f"\nsummary: {passed} passed, {len(report.failures)} failed (of {len(report.checks)})")
    return "\n".join(lines)


def render_json(report: TriggerQAReport) -> str:
    return json.dumps(
        {
            "ok": report.ok,
            "checks": [
                {"skill": c.skill, "prompt": c.prompt, "should_fire": c.should_fire, "fired": c.fired}
                for c in report.checks
            ],
        },
        indent=2,
    )


def run_trigger_qa(*, corpus_path: Path = CORPUS_PATH, skills_dir: Path = DEFAULT_SKILLS_DIR) -> TriggerQAReport:
    checks: list[TriggerCheck] = []
    for entry in load_corpus(corpus_path):
        skill = entry["skill"]
        patterns = _keyword_patterns(skill, skills_dir)
        checks.extend(
            TriggerCheck(skill, prompt, should_fire=True, fired=_fires(prompt, patterns))
            for prompt in entry.get("should_fire", [])
        )
        checks.extend(
            TriggerCheck(skill, prompt, should_fire=False, fired=_fires(prompt, patterns))
            for prompt in entry.get("should_not_fire", [])
        )
    return TriggerQAReport(checks=tuple(checks))
