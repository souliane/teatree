"""Fitness function: the gitlab -> slack coupling stays EXPLICIT (finding D7).

``teatree.backends`` was a single tach node, so the one cross-backend coupling
(``gitlab/sync.py`` calls into ``slack/review_sync.py`` to attach review
permalinks) was invisible in the dependency graph. The backends node is now
split into the aggregator parent + the concrete backends (github / gitlab /
slack) + the shared primitives, so that coupling is a DECLARED edge.

This test guards the ``tach.toml`` declaration so the edge cannot be re-hidden
(by collapsing the submodules back into one node, by dropping the declared
``gitlab -> slack`` edge, or by giving slack an outgoing edge to a sibling
backend). ``uv run tach check`` is the runtime gate that parses the actual import
graph; this pins the declaration the same way ``test_tach_cycle_ratchet.py``
pins the acyclic invariant.
"""

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TACH = _REPO_ROOT / "tach.toml"

_GITLAB = "teatree.backends.gitlab"
_SLACK = "teatree.backends.slack"
_GITHUB = "teatree.backends.github"
_PARENT = "teatree.backends"


def _modules() -> list[dict]:
    return tomllib.loads(_TACH.read_text(encoding="utf-8"))["modules"]


def _entry(path: str) -> dict:
    return next(m for m in _modules() if m["path"] == path)


def _depends(path: str) -> set[str]:
    return set(_entry(path).get("depends_on", []))


class TestBackendsAreSeparateNodes:
    def test_concrete_backends_are_declared_modules(self) -> None:
        paths = {m["path"] for m in _modules()}
        assert {_GITLAB, _SLACK, _GITHUB} <= paths


class TestGitlabSlackCouplingIsExplicit:
    def test_gitlab_declares_the_slack_edge(self) -> None:
        assert _SLACK in _depends(_GITLAB), (
            "The gitlab -> slack review-permalink coupling must be a declared edge; "
            "re-hiding it (collapsing the submodules or dropping this edge) defeats D7."
        )


class TestSlackStaysABackendLeaf:
    def test_slack_has_no_outgoing_sibling_backend_edge(self) -> None:
        slack_deps = _depends(_SLACK)
        assert _GITLAB not in slack_deps
        assert _GITHUB not in slack_deps
        assert _PARENT not in slack_deps
