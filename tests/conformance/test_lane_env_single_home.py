"""The session-lane env markers are stated in ONE place under ``tests/`` (#3973).

``session_lane`` reads three env keys and a factory / Agent-SDK runner exports them, so a
fixture that merges into ``os.environ`` inherits the runner's lane instead of stating one.
Under the headless authoring gate — which fails OPEN for every lane but a positively
identified interactive CLI — that inheritance turned ten refuse-cases green by ALLOWING,
and no allow-case noticed, because a uniformly permissive gate passes all of those.

The repair was one pinned key in one fixture. This walk is what keeps it from being one
pinned key: a hand-rolled scrub is invisible when it is wrong, so the durable guard is
that no test names these keys at all outside :mod:`tests._lane_env`, whose pin clears
every marker and asserts the resulting lane.

Prose is exempt — a docstring naming a key documents it rather than driving the env.
"""

import ast

from tests._lane_env import LANE_KEYS
from tests.conformance._src_tree import REPO_ROOT, parsed_modules

TESTS_DIR = REPO_ROOT / "tests"

#: Below this the walk cannot have been reading the real test tree at all.
_MIN_SCANNED_MODULES = 200

#: The one module allowed to name the markers, relative to ``tests/``.
_CANONICAL = "_lane_env.py"


def _prose_nodes(tree: ast.Module) -> set[int]:
    """The string constants that are docstrings or bare expressions, not operative values."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }


def lane_key_literals(tree: ast.Module) -> set[str]:
    """Every lane marker *tree* names as an operative string constant."""
    prose = _prose_nodes(tree)
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in LANE_KEYS
        and id(node) not in prose
    }


def _offenders() -> dict[str, set[str]]:
    return {
        str(path.relative_to(TESTS_DIR)): keys
        for path, tree in parsed_modules(TESTS_DIR)
        if str(path.relative_to(TESTS_DIR)) != _CANONICAL and (keys := lane_key_literals(tree))
    }


class TestTheWalkCanSeeWhatItLooksFor:
    """Without this the emptiness below would also be what a broken walk reports."""

    def test_the_canonical_module_does_name_every_marker(self) -> None:
        canonical = next(tree for path, tree in parsed_modules(TESTS_DIR) if path.name == _CANONICAL)
        assert lane_key_literals(canonical) == set(LANE_KEYS)

    def test_the_walk_reaches_the_whole_test_tree(self) -> None:
        assert len(parsed_modules(TESTS_DIR)) >= _MIN_SCANNED_MODULES


class TestNoTestHandRollsTheLaneEnv:
    def test_the_markers_are_named_nowhere_else(self) -> None:
        assert _offenders() == {}, (
            "state the lane through tests._lane_env.pinned_lane instead — a hand-rolled "
            "scrub that clears a subset silently inherits the runner's lane"
        )
