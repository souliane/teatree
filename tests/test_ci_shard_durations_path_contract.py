"""The shard's relative ``--durations-path`` only survives ``--rm`` because cwd IS the bind mount (#4483).

``docker run --rm -v "$PWD":/app`` gives the container a writable view of the runner
workspace at ``/app``, and ``dev/Dockerfile.test``'s ``base`` stage sets ``WORKDIR /app``.
So ``--durations-path dev/.test_durations`` resolves to ``/app/dev/.test_durations`` — a
path the runner keeps after the container exits, which is why ``durations-shard-*``
artifacts publish at all.

Nothing pinned that chain, and every link is edited independently: swapping the shard to a
different image (``deploy/Dockerfile``'s own WORKDIR sits outside the mount), retargeting
the build stage, moving the mount, or adding a ``-w`` would each silently redirect the recorded
durations into the container's own layer. The file would then die with the container while
the upload step still finds the COMMITTED ``dev/.test_durations`` at that path on the runner
and republishes month-old timings as freshly measured — green at every step, with the
staleness surfacing only as a slow, badly bin-packed shard lane.

#4483 reported exactly this as the live root cause. It was not: the shard image is
``dev/Dockerfile.test --target base`` (WORKDIR ``/app``), not ``deploy/Dockerfile``, so the
relative path already lands in the mount. These assertions pin the invariant that made that
diagnosis wrong, so a future change cannot quietly make it right.

Sibling guard: ``tests/test_ci_artifact_hidden_files.py`` covers the UPLOAD of that file
(the dotfile drop that was the real #4584 cause). This one covers its PRODUCTION.
"""

import re
from pathlib import Path
from typing import Any, NamedTuple, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_SHARD_JOB = "test-shard"
_IMAGE_OUTPUT_REF = "needs.build-image.outputs.image"


class ShardRun(NamedTuple):
    """The ``docker run`` that records the shard's durations."""

    command: str

    @property
    def mount_targets(self) -> tuple[str, ...]:
        return tuple(re.findall(r"-v\s+\"?\$PWD\"?:([^\s:]+)", self.command))

    @property
    def workdir_override(self) -> str | None:
        found = re.search(r"(?:^|\s)(?:-w|--workdir)[=\s]+(\S+)", self.command)
        return found.group(1) if found else None

    @property
    def durations_path(self) -> str | None:
        found = re.search(r"--durations-path[=\s]+(\S+)", self.command)
        return found.group(1) if found else None


def _load_ci() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8")))


def _steps(job_name: str) -> list[dict[str, Any]]:
    job = cast("dict[str, Any]", _load_ci().get("jobs") or {}).get(job_name) or {}
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def _shard_run() -> ShardRun:
    for step in _steps(_SHARD_JOB):
        run = str(step.get("run") or "")
        if "docker run" in run and "--durations-path" in run:
            return ShardRun(run)
    missing = f"No `docker run` carrying --durations-path in the {_SHARD_JOB} job of {_CI_WORKFLOW.name}."
    raise AssertionError(missing)


def _shard_image_build() -> tuple[Path, str]:
    """The Dockerfile + ``--target`` stage behind ``needs.build-image.outputs.image``."""
    for step in _steps("build-image"):
        found = re.search(r"docker build\s+-f\s+(\S+)\s+--target\s+(\S+)", str(step.get("run") or ""))
        if found:
            return _REPO_ROOT / found.group(1), found.group(2)
    missing = "The build-image job no longer runs a `docker build -f <file> --target <stage>`."
    raise AssertionError(missing)


def _effective_workdir(dockerfile: Path, stage: str) -> str | None:
    """WORKDIR for ``stage``, inherited through ``FROM <stage> AS ...`` when it sets none."""
    parents: dict[str, str] = {}
    workdirs: dict[str, str] = {}
    current = ""
    for raw in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if from_stage := re.match(r"FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", line, re.IGNORECASE):
            current = (from_stage.group(2) or "").lower()
            parents[current] = from_stage.group(1).lower()
        elif workdir := re.match(r"WORKDIR\s+(\S+)\s*$", line, re.IGNORECASE):
            workdirs[current] = workdir.group(1)
    seen: set[str] = set()
    name = stage.lower()
    while name and name not in seen:
        if name in workdirs:
            return workdirs[name]
        seen.add(name)
        name = parents.get(name, "")
    return None


class TestShardDurationsPathSurvivesTheContainer:
    def test_the_shard_durations_path_is_relative(self) -> None:
        """Anti-vacuity: an absolute path would satisfy every assertion below trivially."""
        path = _shard_run().durations_path
        assert path is not None, "The shard docker run carries no --durations-path at all."
        assert not path.startswith("/"), (
            f"--durations-path is {path!r}. If it was deliberately made absolute the container's "
            "cwd stopped mattering — retire this guard rather than letting it pass vacuously."
        )

    def test_the_shard_runs_the_image_whose_workdir_is_pinned(self) -> None:
        assert _IMAGE_OUTPUT_REF in _shard_run().command, (
            f"The {_SHARD_JOB} docker run no longer uses {_IMAGE_OUTPUT_REF}, so the WORKDIR asserted "
            "below is not the one it runs under. deploy/Dockerfile's WORKDIR sits outside the mount, "
            "which would send the recorded durations into the container layer and lose them on --rm (#4483)."
        )

    def test_the_shard_image_stage_sets_the_mount_point_as_workdir(self) -> None:
        dockerfile, stage = _shard_image_build()
        workdir = _effective_workdir(dockerfile, stage)
        assert workdir in _shard_run().mount_targets, (
            f"{dockerfile.name} stage {stage!r} has WORKDIR {workdir!r}, which is not a bind-mount "
            f"target of the {_SHARD_JOB} docker run ({_shard_run().mount_targets}). Relative output "
            "paths would resolve into the container's own layer and die with it (#4483)."
        )

    def test_the_shard_run_does_not_override_the_workdir(self) -> None:
        override = _shard_run().workdir_override
        assert override is None or override in _shard_run().mount_targets, (
            f"The {_SHARD_JOB} docker run passes -w/--workdir {override!r}, outside the bind mount "
            f"{_shard_run().mount_targets}. Relative output paths would no longer reach the runner (#4483)."
        )

    def test_the_recorded_durations_land_inside_the_bind_mount(self) -> None:
        run = _shard_run()
        dockerfile, stage = _shard_image_build()
        cwd = run.workdir_override or _effective_workdir(dockerfile, stage)
        resolved = f"{cwd}/{run.durations_path}"
        assert any(resolved.startswith(f"{target}/") for target in run.mount_targets), (
            f"--durations-path {run.durations_path!r} resolves to {resolved!r}, outside the bind mount "
            f"{run.mount_targets}. The file would be written into the container and destroyed by --rm, "
            "while the upload step republished the committed one from the runner as freshly measured (#4483)."
        )
