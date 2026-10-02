"""The ``--overrides`` argument every ``uv tool install`` of teatree must carry.

``claude-agent-sdk==0.2.152`` declares ``mcp<3.0.0``, which already admits teatree's
``mcp>=2,<3``, so the current override set is empty. ``uv tool install`` still does not
read the installed package's ``[tool.uv] override-dependencies`` table.

The override set is therefore committed as a requirements file
(:data:`UV_OVERRIDES_FILENAME`) that every install site passes explicitly. This module is
the one place that builds the flag, so the three code paths that reinstall teatree
(``t3 update``'s :mod:`teatree.self_update`, the dep-drift repair plan, and the editable
receipt repair) remain ready for a future override without drifting apart.
"""

from pathlib import Path

#: Repo-root requirements file holding the same entries as ``[tool.uv]
#: override-dependencies``. Kept in sync by
#: ``tests/test_claude_agent_sdk_pin.py::TestTheOverrideReachesEveryInstallSurface``.
UV_OVERRIDES_FILENAME = "uv-overrides.txt"


def uv_overrides_args(checkout: Path) -> list[str]:
    """``["--overrides", "<checkout>/uv-overrides.txt"]`` — empty when the file is absent.

    Every caller installs ``--editable <checkout>``, so the file ships with the source
    being installed. It degrades to no flag for a checkout predating the file rather than
    pointing ``uv`` at a path that does not exist, which would turn a recoverable
    reinstall into a hard resolver error on the very path that repairs a broken install.
    """
    overrides = checkout / UV_OVERRIDES_FILENAME
    return ["--overrides", str(overrides)] if overrides.is_file() else []
