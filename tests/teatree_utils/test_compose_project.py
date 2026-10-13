"""One compose project per environment, whichever venue resolves it.

``deploy/t3`` (bash), the host hooks (stdlib only) and ``teatree`` each resolve the
production stack's compose project. They cannot share code, so they must agree as a
function of the environment, or a hook and the launcher it predicts read different
stacks.
"""

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from _deploy_wrapper_paths import wrapper_assignment

from hooks.scripts import container_visibility
from teatree.utils import compose_project
from teatree.utils.compose_project import production_compose_project

_DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
_PROJECT_ENV = ("TEATREE_COMPOSE_PROJECT", "COMPOSE_PROJECT_NAME")


def _launcher_project(env: dict[str, str]) -> str:
    script = (
        f'. "{_DEPLOY}/generation-topology.sh"\n{wrapper_assignment("COMPOSE_PROJECT")}\nprintf %s "$COMPOSE_PROJECT"'
    )
    clean = {key: value for key, value in os.environ.items() if key not in _PROJECT_ENV}
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-c", script],
        env={**clean, **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")
@pytest.mark.parametrize(
    ("env", "expected"),
    [
        pytest.param({}, "teatree", id="nothing_set"),
        pytest.param({"COMPOSE_PROJECT_NAME": "named"}, "named", id="compose_name"),
        pytest.param({"TEATREE_COMPOSE_PROJECT": "rolled"}, "rolled", id="teatree_project"),
        pytest.param({"TEATREE_COMPOSE_PROJECT": "rolled", "COMPOSE_PROJECT_NAME": "named"}, "rolled", id="both"),
        pytest.param(
            {"TEATREE_COMPOSE_PROJECT": "", "COMPOSE_PROJECT_NAME": "named"}, "named", id="empty_wins_nothing"
        ),
        pytest.param({"TEATREE_COMPOSE_PROJECT": "", "COMPOSE_PROJECT_NAME": ""}, "teatree", id="both_empty"),
    ],
)
def test_the_three_resolvers_agree_on_the_host(monkeypatch: pytest.MonkeyPatch, env: dict, expected: str) -> None:
    for name in _PROJECT_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with patch.object(compose_project, "running_in_container", return_value=False):
        answers = (
            _launcher_project(env),
            container_visibility.production_compose_project(),
            production_compose_project(),
        )
    assert answers == (expected, expected, expected)


def test_in_a_container_it_is_the_default_whatever_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """``deploy/t3`` forwards no project variable, so one set in a container names another stack."""
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "repo-wt42")
    monkeypatch.setenv("TEATREE_COMPOSE_PROJECT", "rolled")
    with patch.object(compose_project, "running_in_container", return_value=True):
        assert production_compose_project() == "teatree"
