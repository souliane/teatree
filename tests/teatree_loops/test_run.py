"""Path↔name helper for a loop's OWN on-disk module (#2513, #2650).

After the one-driver cutover (LOOP-PR-A) :mod:`teatree.loops.run` is a pure
path↔name helper, NOT a dispatch seam — there is no central runner. These pin
the single surviving export :func:`teatree.loops.run.parse_script_loop_name`:
the canonical ``src/teatree/loops/<name>/loop.py`` shape parses to its loop name,
and any other shape raises :class:`UnresolvableScriptError` loudly.
"""

import pytest
from django.test import TestCase

from teatree.loops.registry import iter_loops
from teatree.loops.run import UnresolvableScriptError, parse_script_loop_name


class TestParseScriptLoopName:
    def test_own_module_path_parses_to_its_loop_name(self) -> None:
        assert parse_script_loop_name("src/teatree/loops/inbox/loop.py") == "inbox"
        assert parse_script_loop_name("src/teatree/loops/dispatch/loop.py") == "dispatch"

    @pytest.mark.parametrize(
        "script",
        [
            "src/teatree/loops/run.py",  # a stale shared-runner path
            "src/teatree/loops/inbox/sub/loop.py",  # nested, not the per-loop shape
            "teatree/loops/inbox/loop.py",  # missing the package prefix
            "src/teatree/loops/inbox/scanner.py",  # wrong suffix
            "src/teatree/loops//loop.py",  # empty name
        ],
    )
    def test_non_module_shape_raises_loudly(self, script: str) -> None:
        with pytest.raises(UnresolvableScriptError):
            parse_script_loop_name(script)


class TestRunnerNotInRegistry(TestCase):
    def test_run_module_is_not_a_registered_mini_loop(self) -> None:
        names = {loop.name for loop in iter_loops()}
        assert "run" not in names
