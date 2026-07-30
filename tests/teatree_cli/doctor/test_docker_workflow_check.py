"""Tests for ``_check_docker_workflow_wired`` — verify the containerized t3 wiring (#3232).

The check is surfacing-only (never gates the doctor exit code). It stays silent
inside a container and on a host that never opted into the Docker workflow (no
installed alias block); once opted in, a missing/stale piece the wrapper depends
on is surfaced as a single actionable WARN pointing back at ``t3 setup``.
"""

import io
from contextlib import redirect_stdout
from functools import partial
from pathlib import Path
from unittest.mock import patch

from teatree.cli.doctor import checks_docker
from teatree.cli.doctor.checks_docker import _check_docker_workflow_wired
from teatree.docker.workflow import compose_path, is_running_in_container, render_alias_block, wrapper_path

# A container-marker path guaranteed absent, so container detection in these
# host-scenario checks keys ONLY off the injected env — never the real
# ``/.dockerenv`` marker. That marker EXISTS whenever the suite itself runs inside
# the CI test container (the shard/coverage lanes run pytest under `docker run`),
# which would otherwise make every host check early-return "in a container" and
# swallow the host-path WARN these tests assert.
_ABSENT_DOCKERENV = Path("/nonexistent/teatree-test/.dockerenv")


def _wire_repo(root: Path) -> Path:
    repo = root / "clone"
    (repo / "deploy").mkdir(parents=True)
    compose_path(repo).write_text("name: teatree\n", encoding="utf-8")
    wrapper = wrapper_path(repo)
    wrapper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    wrapper.chmod(0o755)
    return repo


def _run(**kwargs) -> tuple[bool, str]:
    out = io.StringIO()
    # Pin container detection to the injected env alone: ``is_running_in_container``
    # also consults the real ``/.dockerenv``, present when this very suite runs in
    # the CI container — un-pinned, the host-scenario checks below would falsely
    # detect a container and return silently.
    hermetic = partial(is_running_in_container, dockerenv=_ABSENT_DOCKERENV)
    with patch.object(checks_docker, "is_running_in_container", hermetic), redirect_stdout(out):
        ok = _check_docker_workflow_wired(**kwargs)
    return ok, out.getvalue()


class TestDockerWorkflowCheck:
    def test_silent_inside_a_container(self, tmp_path: Path) -> None:
        repo = _wire_repo(tmp_path)
        # Even a broken wiring is not reported when this IS the containerized runtime.
        rc = tmp_path / ".bashrc"
        rc.write_text(render_alias_block(repo), encoding="utf-8")
        ok, msg = _run(env={"TEATREE_ROLE": "worker"}, repo=repo, rc_paths=[rc], which=lambda _t: None)
        assert ok is True
        assert msg == ""

    def test_silent_when_not_opted_in(self, tmp_path: Path) -> None:
        repo = _wire_repo(tmp_path)
        rc = tmp_path / ".bashrc"
        rc.write_text("export FOO=1\n", encoding="utf-8")  # no alias block
        ok, msg = _run(env={}, repo=repo, rc_paths=[rc], which=lambda _t: None)
        assert ok is True
        assert msg == ""

    def test_silent_when_main_clone_unresolved(self, tmp_path: Path) -> None:
        rc = tmp_path / ".bashrc"
        ok, msg = _run(env={}, repo=None, rc_paths=[rc], which=lambda _t: None)
        assert ok is True
        assert msg == ""

    def test_silent_when_opted_in_and_healthy(self, tmp_path: Path) -> None:
        repo = _wire_repo(tmp_path)
        rc = tmp_path / ".bashrc"
        rc.write_text(render_alias_block(repo), encoding="utf-8")
        ok, msg = _run(env={}, repo=repo, rc_paths=[rc], which=lambda _t: "/usr/bin/docker")
        assert ok is True
        assert "WARN" not in msg

    def test_warns_when_opted_in_but_docker_missing(self, tmp_path: Path) -> None:
        repo = _wire_repo(tmp_path)
        rc = tmp_path / ".bashrc"
        rc.write_text(render_alias_block(repo), encoding="utf-8")
        ok, msg = _run(env={}, repo=repo, rc_paths=[rc], which=lambda _t: None)
        assert ok is True  # surfacing-only — never gates the exit code
        assert "WARN" in msg
        assert "docker" in msg
        assert "t3 setup" in msg
