# test-path: cross-cutting — the directive surfaces span hook_router.py (hooks/) and teatree.cli.loop.
"""No loop surface teaches a session the in-session claim-and-spawn cycle; each names the worker.

The ``t3 worker`` claims every pending task and runs it headlessly, so a surface that told a
session to ``claim-next`` and spawn a sub-agent would make it a second claimant racing the
worker's drain. The SessionStart and PreCompact texts are pinned in
``tests/test_worker_is_the_only_queue_drain.py``; these are the ``t3 loop`` CLI surfaces.
"""

import pytest

from teatree.cli.loop import app as loop_cli

_SURFACES = {
    "loop_cli_help": loop_cli.loop_app.info.help or "",
    "loop_cli_module_docstring": loop_cli.__doc__ or "",
    "loop_start_docstring": loop_cli.start_command.__doc__ or "",
}


@pytest.mark.parametrize("surface", _SURFACES.values(), ids=list(_SURFACES))
def test_the_surface_names_the_worker_and_no_in_session_claim(surface: str) -> None:
    assert "t3 worker" in surface
    assert "claim-next" not in surface
