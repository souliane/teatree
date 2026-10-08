"""The CI workflow contract: least-privilege tokens, pinned actions, main-built images, a force-push alert."""

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests._actions_workflow import WORKFLOWS, github_context, load, render, step_runs, triggers

_BASH = shutil.which("bash") or "/bin/bash"
_FILES = sorted(WORKFLOWS.glob("*.yml"))
_REUSABLE = frozenset({"eval-pr-reusable.yml", "eval-weekly-reusable.yml"})
_RANK = {"none": 0, "read": 1, "write": 2}
_TOKEN_USE = re.compile(r"github\.token|secrets\.GITHUB_TOKEN")

type JobKey = tuple[str, str]


@cache
def _workflow(name: str) -> dict[str, Any]:
    assert (WORKFLOWS / name).exists(), f"{name} is part of the workflow set"
    return load(name)


def _all_jobs() -> list[tuple[str, str, dict[str, Any]]]:
    return [(path.name, key, job) for path in _FILES for key, job in _workflow(path.name)["jobs"].items()]


def _steps(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def _text(node: object) -> str:
    return yaml.dump(node, width=10**6)


def _by_id(job: Mapping[str, Any], step_id: str) -> dict[str, Any]:
    found = [step for step in _steps(job) if step.get("id") == step_id]
    assert found, f"the job has a step with id {step_id!r}"
    return found[0]


def _top_level_violations(name: str, workflow: Mapping[str, Any]) -> list[str]:
    block = workflow.get("permissions")
    if block is None:
        return [] if name in _REUSABLE else ["no top-level permissions block"]
    if not isinstance(block, Mapping):
        return [f"top-level permissions list scopes, not {block!r}"]
    return [f"top-level {scope}: {level}" for scope, level in block.items() if level not in {"read", "none"}]


@pytest.mark.parametrize("path", _FILES, ids=lambda path: path.name)
def test_every_workflow_defaults_its_token_to_read_only(path: Path) -> None:
    assert not _top_level_violations(path.name, _workflow(path.name))


@pytest.mark.parametrize(
    ("name", "workflow", "violations"),
    [
        ("ci.yml", {}, 1),
        ("ci.yml", {"permissions": {"contents": "read"}}, 0),
        ("ci.yml", {"permissions": {"contents": "read", "issues": "write"}}, 1),
        ("eval-pr-reusable.yml", {}, 0),
        ("eval-weekly-reusable.yml", {"permissions": {"contents": "read"}}, 0),
        ("eval-weekly-reusable.yml", {"permissions": {"contents": "write"}}, 1),
    ],
)
def test_a_reusable_workflow_may_carry_a_read_only_block_but_needs_none(
    name: str, workflow: dict[str, Any], violations: int
) -> None:
    assert len(_top_level_violations(name, workflow)) == violations


_MINIMUM_GRANTS: dict[JobKey, dict[str, str]] = {
    ("ci.yml", "lint"): {"contents": "read", "packages": "read"},
    ("ci.yml", "build-image"): {"contents": "read", "packages": "write"},
    ("ci.yml", "test-shard"): {"contents": "read", "packages": "read"},
    ("ci.yml", "jscpd-scan"): {"contents": "read", "packages": "read"},
    ("ci.yml", "mutation-full"): {"contents": "read", "pull-requests": "read"},
    ("ci.yml", "selection-audit"): {"contents": "read", "issues": "write"},
    ("dependabot-sbom-sync.yml", "dependabot-sbom-sync"): {"contents": "read", "actions": "read"},
    ("docs.yml", "deploy"): {"contents": "read", "pages": "write", "id-token": "write"},
    ("eval.yml", "prepare"): {"contents": "read", "pull-requests": "read"},
    ("eval-nightly.yml", "prepare"): {"contents": "read", "pull-requests": "read"},
    ("force-push-alert.yml", "alert"): {"contents": "read"},
    ("lock-refresh-scope.yml", "scope"): {"contents": "write", "pull-requests": "write"},
    ("needs-triage.yml", "label"): {"issues": "write"},
    ("publish-image.yml", "publish"): {"contents": "read", "packages": "write"},
}
_CALLER_BOUNDED: frozenset[JobKey] = frozenset({("eval-weekly-reusable.yml", "prepare")})


def _shortfalls(workflow: Mapping[str, Any], job: Mapping[str, Any], wanted: Mapping[str, str]) -> list[str]:
    held = job.get("permissions", workflow.get("permissions")) or {}
    return [f"{scope}: {level}" for scope, level in wanted.items() if _RANK[held.get(scope, "none")] < _RANK[level]]


@pytest.mark.parametrize(("name", "key"), list(_MINIMUM_GRANTS), ids=str)
def test_a_token_using_job_holds_at_least_its_scopes(name: str, key: str) -> None:
    workflow = _workflow(name)
    assert not _shortfalls(workflow, workflow["jobs"][key], _MINIMUM_GRANTS[name, key])


def test_every_job_that_hands_the_workflow_token_to_a_step_is_accounted_for() -> None:
    holders = {(name, key) for name, key, job in _all_jobs() if _TOKEN_USE.search(_text(job))}
    assert holders <= set(_MINIMUM_GRANTS) | _CALLER_BOUNDED


_USES = re.compile(r"^[ \t-]*uses:\s*(?P<action>[^\s@]+)@(?P<ref>\S+?)(?:\s+#\s*(?P<label>\S+))?\s*$", re.MULTILINE)


def _pins() -> list[tuple[str, str, str, str | None]]:
    return [
        (path.name, found["action"], found["ref"], found["label"])
        for path in _FILES
        for found in _USES.finditer(path.read_text(encoding="utf-8"))
    ]


def test_every_action_is_pinned_to_a_full_commit_sha() -> None:
    assert not [(name, action) for name, action, ref, _ in _pins() if not re.fullmatch(r"[0-9a-f]{40}", ref)]


def test_every_pinned_sha_carries_one_version_label_tree_wide() -> None:
    labels: dict[tuple[str, str], set[str | None]] = {}
    for _name, action, ref, label in _pins():
        labels.setdefault((action, ref), set()).add(label)
    assert not {pin: seen for pin, seen in labels.items() if len(seen) != 1 or None in seen}


def test_actions_come_from_the_reviewed_publishers_only() -> None:
    reviewed = {"astral-sh/setup-uv", "nick-fields/retry"}
    assert not [
        action
        for _name, action, _ref, _label in _pins()
        if not action.startswith("actions/") and action not in reviewed
    ]


_PERSISTING: frozenset[JobKey] = frozenset(
    {
        ("ci.yml", "refresh-durations"),
        ("uv-lock-upgrade.yml", "refresh-lockfile"),
        ("eval.yml", "publish"),
        ("eval-weekly-reusable.yml", "publish"),
        ("eval-pr-reusable.yml", "detect"),
    }
)


def _checkouts() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (name, key, step.get("with") or {})
        for name, key, job in _all_jobs()
        for step in _steps(job)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]


def test_every_checkout_states_whether_it_persists_credentials() -> None:
    assert not [
        (name, key) for name, key, inputs in _checkouts() if not isinstance(inputs.get("persist-credentials"), bool)
    ]


def test_only_the_jobs_that_push_persist_credentials() -> None:
    persisting = {(name, key) for name, key, inputs in _checkouts() if inputs.get("persist-credentials") is True}
    assert persisting == _PERSISTING
    assert not [
        (name, key) for name, key, inputs in _checkouts() if "repository" in inputs and inputs["persist-credentials"]
    ]


def _plain(value: object) -> str:
    if value is None:
        return ""
    return str(value).lower() if isinstance(value, bool) else str(value)


def _run_build_image(event: str, tmp_path: Path, *, tags_exist: bool) -> tuple[list[str], dict[str, str]]:
    job = _workflow("ci.yml")["jobs"]["build-image"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        '#!/bin/sh\necho "$*" >> "$DOCKER_LOG"\n'
        'case "$1 $2" in\n'
        '  "manifest inspect") exit "$MANIFEST_EXIT" ;;\n'
        '  "inspect --format") printf "%s@sha256:%064d\\n" "${4%%:*}" 1 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()
    steps: dict[str, Any] = {}
    for step_id in ("meta", "test_image", "lint_image"):
        step = _by_id(job, step_id)
        context = {"github": github_context(event), "steps": steps}
        if not step_runs(step, context, earlier_step_failed=False, cancelled=False):
            continue
        fixed = {"KEY": "k1", "KEY_LINT": "k2"}
        env = {name: fixed.get(name) or _plain(render(value, context)) for name, value in step.get("env", {}).items()}
        github_output = tmp_path / f"{step_id}.out"
        github_output.touch()
        subprocess.run(
            [_BASH, "-e", "-c", step["run"]],
            env={
                **env,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "DOCKER_LOG": str(log),
                "MANIFEST_EXIT": "0" if tags_exist else "1",
                "GITHUB_OUTPUT": str(github_output),
            },
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        steps[step_id] = {"outputs": dict(line.split("=", 1) for line in github_output.read_text().splitlines())}
    context = {"steps": steps}
    return log.read_text().splitlines(), {
        name: _plain(render(value, context)) for name, value in job["outputs"].items()
    }


def _pushed(calls: list[str]) -> list[str]:
    return [call.split()[1] for call in calls if call.startswith("push ")]


def test_a_pull_request_that_finds_the_shared_tags_builds_nothing(tmp_path: Path) -> None:
    calls, outputs = _run_build_image("pull_request", tmp_path, tags_exist=True)
    assert [call.split()[0] for call in calls] == ["manifest", "manifest"]
    assert (outputs["image"], outputs["image_lint"]) == ("ghcr.io/owner/repo-test:k1", "ghcr.io/owner/repo-lint:k2")


def test_a_pull_request_that_misses_the_shared_tags_pushes_them(tmp_path: Path) -> None:
    calls, outputs = _run_build_image("pull_request", tmp_path, tags_exist=False)
    assert _pushed(calls) == ["ghcr.io/owner/repo-test:k1", "ghcr.io/owner/repo-lint:k2"]
    assert outputs["image"] == "ghcr.io/owner/repo-test:k1"


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_every_other_event_builds_both_images_under_main_tags_and_hands_out_digests(event: str, tmp_path: Path) -> None:
    calls, outputs = _run_build_image(event, tmp_path, tags_exist=True)
    assert _pushed(calls) == ["ghcr.io/owner/repo-test:main-k1", "ghcr.io/owner/repo-lint:main-k2"]
    assert len([call for call in calls if call.startswith("build ")]) == 2
    assert re.fullmatch(r"ghcr\.io/owner/repo-test@sha256:[0-9a-f]{64}", outputs["image"])
    assert re.fullmatch(r"ghcr\.io/owner/repo-lint@sha256:[0-9a-f]{64}", outputs["image_lint"])


_CREDENTIAL = re.compile(r"secrets\.(?:TEATREE_GH_TOKEN|DEPLOY_SSH_\w+|T3_ADMIN_USER|GIT_AUTHOR_\w+)\b")
_PRODUCTION: frozenset[JobKey] = frozenset(
    {
        ("deploy.yml", "deploy"),
        ("ci.yml", "refresh-durations"),
        ("uv-lock-upgrade.yml", "refresh-lockfile"),
        ("dependabot-sbom-sync.yml", "dependabot-sbom-sync"),
        ("eval.yml", "publish"),
    }
)


def _environment(job: Mapping[str, Any]) -> object:
    declared = job.get("environment")
    return declared.get("name") if isinstance(declared, Mapping) else declared


def test_the_jobs_that_hold_deploy_or_push_credentials_are_exactly_the_production_ones() -> None:
    assert {(name, key) for name, key, job in _all_jobs() if _CREDENTIAL.search(_text(job))} == _PRODUCTION


@pytest.mark.parametrize(("name", "key"), sorted(_PRODUCTION), ids=str)
def test_credential_jobs_bind_the_production_environment(name: str, key: str) -> None:
    assert _environment(_workflow(name)["jobs"][key]) == "production"


def test_the_dashboard_publish_job_runs_on_main_only() -> None:
    condition = " ".join(str(_workflow("eval.yml")["jobs"]["publish"]["if"]).split())
    assert condition == "always() && needs.prepare.outputs.run_eval == 'true' && github.ref == 'refs/heads/main'"


_PR_WRITE = re.compile(r"\bgh pr (?:create|review)\b|pulls\.(?:create|createReview)\b")


def test_no_step_opens_or_reviews_a_pull_request_with_the_workflow_token() -> None:
    offenders = [
        (name, key)
        for name, key, job in _all_jobs()
        for step in _steps(job)
        if _TOKEN_USE.search(_text(step)) and _PR_WRITE.search(_text(step))
    ]
    assert not offenders


def _run_alert(tmp_path: Path, events: list[dict[str, Any]], *, gh_exit: int = 0) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s" "$GH_STUB_JSON"\nexit "$GH_STUB_EXIT"\n', encoding="utf-8")
    gh.chmod(0o755)
    (step,) = _steps(_workflow("force-push-alert.yml")["jobs"]["alert"])
    return subprocess.run(
        [_BASH, "-e", "-c", step["run"]],
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "owner/repo",
            "GH_STUB_JSON": json.dumps(events),
            "GH_STUB_EXIT": str(gh_exit),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def _force_push(hours_ago: float) -> dict[str, Any]:
    when = datetime.now(UTC) - timedelta(hours=hours_ago)
    return {"timestamp": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "actor": {"login": "someone"}}


def test_the_alert_runs_daily_on_demand_and_when_its_own_file_changes() -> None:
    on = triggers(_workflow("force-push-alert.yml"))
    assert on["schedule"]
    assert "workflow_dispatch" in on
    assert on["pull_request"]["paths"] == [".github/workflows/force-push-alert.yml"]


@pytest.mark.parametrize(
    ("events", "gh_exit"),
    [([_force_push(2)], 0), ([], 1)],
    ids=["inside-the-window", "api-error"],
)
def test_the_alert_goes_red_on_a_recent_force_push_and_on_an_api_error(
    events: list[dict[str, Any]], gh_exit: int, tmp_path: Path
) -> None:
    result = _run_alert(tmp_path, events, gh_exit=gh_exit)
    assert result.returncode != 0
    assert "::error::" in result.stdout + result.stderr


@pytest.mark.parametrize("events", [[], [_force_push(30)]], ids=["none", "older-than-the-window"])
def test_the_alert_stays_green_without_a_recent_force_push(events: list[dict[str, Any]], tmp_path: Path) -> None:
    assert _run_alert(tmp_path, events).returncode == 0


def test_the_alert_queries_force_pushes_to_main() -> None:
    (step,) = _steps(_workflow("force-push-alert.yml")["jobs"]["alert"])
    assert "activity_type=force_push&ref=refs/heads/main" in step["run"]
    assert "|| true" not in step["run"]
