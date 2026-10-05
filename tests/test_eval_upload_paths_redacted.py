# test-path: cross-cutting — pins the eval*.yml workflows' redaction + upload contract; no single src/teatree/ mirror.
"""Every eval workflow upload holds only redacted bytes, and the private ones expire.

The redactor guards the writers that call it, so this pins the other half: each
``actions/upload-artifact`` path in an ``eval*.yml`` workflow must be produced by a
redacting writer — the ``t3 eval run`` report flags, a merge's ``--out``, or the
``python -m teatree.eval.artifact_redaction tee`` log filter — and never by a raw
``tee`` / ``>`` / ``>>`` that would copy a credential straight into the artifact.
Every ``t3 eval run`` also streams through the filter (stderr included), so the job
log itself is redacted; a reusable workflow reaches the filter through the installed
package, since it runs in its caller's checkout. The private uploads (transcripts,
reports, raw run logs) carry a short ``retention-days``.

As defense in depth, every upload is immediately preceded by one in-place redaction
pass over the exact files it publishes, run with every credential the job holds, and
ships only when that pass succeeded (it deletes any file it cannot prove clean); every
artifact printed into the job log or the step summary goes through the filter; no
checkout in a job that runs the agent under test leaves its token on disk; and every
``--docker`` run hands the agent a fresh, empty staging directory as its writable
mount, never the reports' own directory (``$RUNNER_TEMP`` also holds the run log).
"""

import dataclasses
import re
from functools import cached_property
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
import typer
import yaml

from teatree.cli.eval.run_docker import RunDockerArgs

_WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("eval*.yml"))
_UPLOAD = "actions/upload-artifact"
_CHECKOUT = "actions/checkout"
_RUNNER_TEMP = "${{ runner.temp }}"
_REDACTING_WRITERS = ("--transcript-html", "--summary-md", "--summary-json", "--out", "artifact_redaction tee")
_RAW_WRITER = re.compile(r"(?:(?<!artifact_redaction )\btee(?:\s+-a)?|>>?)$")
_ASSIGNMENT = re.compile(r'^\s*([A-Z_][A-Z0-9_]*)="([^"]*)"\s*$', re.MULTILINE)
_EXPRESSION = re.compile(r"\$\{\{[^}]*\}\}")
_VARYING = re.compile(r"(\$\{\{[^}]*\}\}|\*)")
_PRIVATE_ARTIFACT = re.compile(r"transcript|report|run-log")
_MAX_PRIVATE_RETENTION_DAYS = 14
_REDACT_TEE = "| uv run python -m teatree.eval.artifact_redaction tee"
_REDACT_IN_PLACE = "uv run python -m teatree.eval.artifact_redaction in-place"
_EVAL_RUN = re.compile(r"uv run t3 eval run\b(?:[^\n]*\\\n)*[^\n]*")
_REPORT_FLAG = re.compile(r'--(transcript-html|summary-md|summary-json) "([^"]+)"')
_SHELL_VARIABLE = re.compile(r"\$\{?[A-Za-z_]\w*\}?")
#: A command that prints a file, in command position (so prose inside an `echo` is not one).
_FILE_READ = re.compile(
    r"(?:^|[;&|({]|\b(?:then|do|else)\b)\s*(?P<read>\b(?:cat|tail|head)\s[^|;&<>\n]*)", re.MULTILINE
)
_INTO_THE_FILTER = re.compile(r"\s*\|\s*uv run python -m teatree\.eval\.artifact_redaction tee\b")
_INTO_A_FILE = re.compile(r"\s*>>?")
_INTO_THE_STEP_SUMMARY = re.compile(r'\s*>>?\s*"?\$\{?GITHUB_STEP_SUMMARY\b')
_PIPEFAIL = re.compile(r"\bset -\w*o pipefail\b")
_PYTHON_TARGET = re.compile(r"\buv run python (?P<target>-m \S+|\S+)")
_INSTALLED_REDACTOR = "-m teatree.eval.artifact_redaction"
_STDERR_INTO_THE_FILTER = re.compile(
    r"2>&1\s*(?:\\\n\s*)?\|\s*uv run python -m teatree\.eval\.artifact_redaction tee\b"
)


def _workflow(path: Path) -> dict[Any, Any]:
    return cast("dict[Any, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _jobs(workflow: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", _workflow(workflow)["jobs"])


def _is_reusable(workflow: Path) -> bool:
    # YAML 1.1 reads the bare `on:` key as the boolean True.
    return "workflow_call" in _workflow(workflow)[True]


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", job.get("steps", []))


def _scripts(job: dict[str, Any]) -> list[str]:
    return [
        script for step in _steps(job) for script in (step.get("run"), step.get("with", {}).get("command")) if script
    ]


def _expanded_scripts(job: dict[str, Any]) -> str:
    """The job's shell, with its env and `NAME="..."` assignments substituted in."""
    env = {**job.get("env", {}), **{k: v for step in _steps(job) for k, v in step.get("env", {}).items()}}
    text = "\n".join(_scripts(job)).replace(_RUNNER_TEMP, "$RUNNER_TEMP")
    variables = {
        name: _EXPRESSION.sub("expr", str(value).replace(_RUNNER_TEMP, "$RUNNER_TEMP")) for name, value in env.items()
    }
    variables |= dict(_ASSIGNMENT.findall(text))
    for _ in range(3):
        for name, value in variables.items():
            text = re.sub(rf"\$(?:\{{{name}\}}|{name}\b)", lambda _match, value=value: value, text)
    return text


def _path_pattern(upload_path: str) -> re.Pattern[str]:
    parts = _VARYING.split(upload_path.replace(_RUNNER_TEMP, "$RUNNER_TEMP"))
    return re.compile("".join(r'[^\s"/]*' if _VARYING.fullmatch(part) else re.escape(part) for part in parts))


def _producers(script: str, pattern: re.Pattern[str]) -> list[str]:
    return [script[: match.start()].rstrip().rstrip('"').rstrip() for match in pattern.finditer(script)]


def _unfiltered_reads(script: str) -> list[str]:
    """Each file the script prints into the job log or the step summary without the redacting filter.

    A copy into another file on the runner is not one: that file leaves only through
    a later print or upload, which is checked on its own.
    """
    unfiltered = []
    for match in _FILE_READ.finditer(script):
        sink = script[match.end() :]
        if _INTO_THE_FILTER.match(sink) or (_INTO_A_FILE.match(sink) and not _INTO_THE_STEP_SUMMARY.match(sink)):
            continue
        unfiltered.append(match["read"].strip())
    return unfiltered


def _is_upload(step: dict[str, Any]) -> bool:
    return str(step.get("uses", "")).startswith(_UPLOAD)


def _runs_the_agent(job: dict[str, Any]) -> bool:
    return any("t3 eval run" in script for script in _scripts(job))


@dataclasses.dataclass(frozen=True)
class _Upload:
    label: str
    job: dict[str, Any]
    index: int

    @property
    def step(self) -> dict[str, Any]:
        return _steps(self.job)[self.index]

    @property
    def path(self) -> str:
        return cast("str", self.step["with"]["path"])

    @cached_property
    def script(self) -> str:
        return _expanded_scripts(self.job)

    @property
    def redaction_pass(self) -> dict[str, Any]:
        """The step right before the run of consecutive upload steps this one belongs to."""
        start = self.index
        while start > 0 and _is_upload(_steps(self.job)[start - 1]):
            start -= 1
        return _steps(self.job)[start - 1]


_UPLOADS = [
    _Upload(f"{workflow.name}:{name}:{step['with']['name']}", job, index)
    for workflow in _WORKFLOWS
    for name, job in _jobs(workflow).items()
    for index, step in enumerate(_steps(job))
    if _is_upload(step)
]
_IDS = [upload.label for upload in _UPLOADS]


def test_the_scan_sees_every_eval_upload() -> None:
    assert len(_UPLOADS) >= 12


@pytest.mark.parametrize("upload", _UPLOADS, ids=_IDS)
def test_every_uploaded_path_comes_from_a_redacting_writer(upload: _Upload) -> None:
    producers = _producers(upload.script, _path_pattern(upload.path))
    raw = [prefix[-60:] for prefix in producers if _RAW_WRITER.search(prefix)]
    redacting = [prefix for prefix in producers if prefix.endswith(_REDACTING_WRITERS)]

    assert not raw, f"{upload.label}: {upload.path} is written by a raw tee/redirect: {raw}"
    assert redacting, f"{upload.label}: nothing redacting writes {upload.path} (expected one of {_REDACTING_WRITERS})"


@pytest.mark.parametrize("upload", _UPLOADS, ids=_IDS)
def test_every_upload_is_redacted_in_place_right_before_it_ships(upload: _Upload) -> None:
    redaction = upload.redaction_pass
    script = _expanded_scripts({"steps": [redaction]})

    assert _REDACT_IN_PLACE in script, f"{upload.label}: the step before the upload is not the in-place redaction"
    assert redaction.get("if") == "always()", f"{upload.label}: the redaction pass must run on a red job too"
    assert _path_pattern(upload.path).search(script), f"{upload.label}: the redaction pass skips {upload.path}"


@pytest.mark.parametrize("upload", _UPLOADS, ids=_IDS)
def test_an_upload_ships_only_when_its_redaction_pass_succeeded(upload: _Upload) -> None:
    # The pass deletes a file it cannot prove clean and exits non-zero; gating on its
    # outcome keeps the upload from shipping whatever else that run left behind.
    step_id = upload.redaction_pass.get("id")

    assert step_id, f"{upload.label}: the redaction pass has no `id`, so the upload cannot be gated on it"
    assert upload.step.get("if") == f"always() && steps.{step_id}.outcome == 'success'", (
        f"{upload.label}: the upload must run on a red job, but only once its redaction pass succeeded"
    )


def _credentials_missing_from(step: dict[str, Any], job: dict[str, Any]) -> list[str]:
    held = {
        name: value for other in _steps(job) for name, value in other.get("env", {}).items() if "secrets." in str(value)
    }
    return sorted(name for name, value in held.items() if step.get("env", {}).get(name) != value)


@pytest.mark.parametrize("upload", _UPLOADS, ids=_IDS)
def test_the_redaction_pass_holds_every_credential_the_job_holds(upload: _Upload) -> None:
    missing = _credentials_missing_from(upload.redaction_pass, upload.job)

    assert not missing, f"{upload.label}: the redaction pass cannot redact {missing} — it does not hold them"


@pytest.mark.parametrize(
    "upload", [u for u in _UPLOADS if _PRIVATE_ARTIFACT.search(u.step["with"]["name"])], ids=lambda u: u.label
)
def test_a_private_upload_expires(upload: _Upload) -> None:
    days = upload.step["with"].get("retention-days")

    assert days is not None, f"{upload.label}: private artifact sets no retention-days"
    assert 1 <= int(days) <= _MAX_PRIVATE_RETENTION_DAYS


@pytest.mark.parametrize("workflow", _WORKFLOWS, ids=lambda path: path.name)
def test_every_eval_run_streams_through_the_redacting_filter(workflow: Path) -> None:
    for name, job in _jobs(workflow).items():
        for script in _scripts(job):
            for invocation in _EVAL_RUN.finditer(script):
                assert _REDACT_TEE in invocation.group(), (
                    f"{workflow.name}:{name}: `t3 eval run` writes to the job log unfiltered — pipe it "
                    f"through `{_REDACT_TEE.removeprefix('| ')}`: {invocation.group()!r}"
                )
                assert _STDERR_INTO_THE_FILTER.search(invocation.group()), (
                    f"{workflow.name}:{name}: stderr (tracebacks, provider errors) must join the filtered "
                    f"stream with `2>&1` before the pipe: {invocation.group()!r}"
                )
                assert _PIPEFAIL.search(script), f"{workflow.name}:{name}: the filter must not mask the eval's exit"


@pytest.mark.parametrize("workflow", [w for w in _WORKFLOWS if _is_reusable(w)], ids=lambda path: path.name)
def test_a_reusable_workflow_reaches_the_redactor_through_the_installed_package(workflow: Path) -> None:
    # A reusable workflow runs in its CALLER's checkout: an overlay has no
    # scripts/eval/, so a repo-relative redaction script exits 2 and leaves no log.
    targets = {
        match["target"]
        for job in _jobs(workflow).values()
        for script in _scripts(job)
        for match in _PYTHON_TARGET.finditer(script)
        if "redact" in match["target"]
    }

    assert targets == {_INSTALLED_REDACTOR}, f"{workflow.name}: redaction must run as `{_INSTALLED_REDACTOR}`"


@pytest.mark.parametrize("workflow", _WORKFLOWS, ids=lambda path: path.name)
def test_no_eval_artifact_reaches_the_job_log_or_step_summary_unfiltered(workflow: Path) -> None:
    for name, job in _jobs(workflow).items():
        for step in _steps(job):
            script = "\n".join(_scripts({"steps": [step]}))
            unfiltered = _unfiltered_reads(script)
            assert not unfiltered, (
                f"{workflow.name}:{name}: these print an eval artifact into the job log or the step summary "
                f"unfiltered — pipe each through `{_REDACT_TEE.removeprefix('| ')}`: {unfiltered}"
            )
            if not any(_INTO_THE_FILTER.match(script[read.end() :]) for read in _FILE_READ.finditer(script)):
                continue
            assert _PIPEFAIL.search(script), f"{workflow.name}:{name}: a filter crash must turn the step red"
            # The eval run itself must not hold the token pool (the agent would get it);
            # any later step that prints an artifact holds what the redaction pass holds.
            if "t3 eval run" not in script:
                missing = _credentials_missing_from(step, job)
                assert not missing, f"{workflow.name}:{name}: the filter cannot redact {missing} — the step lacks them"


@pytest.mark.parametrize("workflow", _WORKFLOWS, ids=lambda path: path.name)
def test_no_checkout_in_an_agent_job_leaves_its_token_on_disk(workflow: Path) -> None:
    persisting = [
        f"{name}:{step.get('name', step['uses'])}"
        for name, job in _jobs(workflow).items()
        if _runs_the_agent(job)
        for step in _steps(job)
        if str(step.get("uses", "")).startswith(_CHECKOUT)
        and step.get("with", {}).get("persist-credentials") is not False
    ]

    assert not persisting, (
        f"{workflow.name}: these checkouts persist the job token where the agent can read it: {persisting}"
    )


_DOCKER_RUNS = [
    (f"{workflow.name}:{name}", dict(_REPORT_FLAG.findall(invocation.group())))
    for workflow in _WORKFLOWS
    for name, job in _jobs(workflow).items()
    for invocation in _EVAL_RUN.finditer(_expanded_scripts(job))
    if "--docker" in invocation.group()
]


def test_the_scan_sees_every_docker_eval_run() -> None:
    assert len(_DOCKER_RUNS) >= 7
    assert all(reports for _label, reports in _DOCKER_RUNS)


@pytest.mark.parametrize(("label", "reports"), _DOCKER_RUNS, ids=[label for label, _reports in _DOCKER_RUNS])
def test_a_docker_eval_run_mounts_only_a_fresh_staging_dir(label: str, reports: dict[str, str], tmp_path: Path) -> None:
    runner_temp = tmp_path / "runner-temp"
    paths = {
        flag.replace("-", "_"): Path(_SHELL_VARIABLE.sub("leg", value.replace("$RUNNER_TEMP", str(runner_temp))))
        for flag, value in reports.items()
    }
    report_dirs = {path.parent for path in paths.values()}
    for directory in report_dirs:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "eval-run-leg.log").write_text("attempt 1\n", encoding="utf-8")
    mounts: list[tuple[Path, list[Path]]] = []

    def run(_argv: list[str], *, artifacts_dir: Path) -> int:
        mounts.append((artifacts_dir, sorted(artifacts_dir.iterdir())))
        return 0

    args = RunDockerArgs(
        name=None,
        lane=None,
        surface=None,
        shard=None,
        output_format="text",
        max_turns=None,
        max_budget_usd=1.0,
        effort="high",
        trials=1,
        require="any",
        models=None,
        backend="api",
        require_executed=True,
        parallel=1,
        **cast("dict[str, Any]", paths),
    )
    with patch("teatree.cli.eval.run_docker.run_eval_in_docker", side_effect=run), pytest.raises(typer.Exit):
        args.dispatch()

    [(mount, contents)] = mounts
    assert mount not in report_dirs, f"{label}: the agent gets the reports' own directory as its writable mount"
    assert mount.parent in report_dirs
    assert contents == [], f"{label}: the staging mount is not empty: {contents}"
    assert not mount.exists(), f"{label}: the staging mount outlives the run"
