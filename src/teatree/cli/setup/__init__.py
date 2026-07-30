"""t3 setup — first-time and ongoing global skill installation.

Coordination lives in :mod:`teatree.cli.setup.command`, which composes the
per-concern units under this package: clone resolution (:mod:`.clone`), global
``t3`` install (:mod:`.tool_installer`), APM (:mod:`.apm`), skill linking
(:mod:`.skill_linker`), and Claude-plugin registration (:mod:`.plugin_registrar`).
"""

from teatree.cli.doctor import AGENT_SKILL_RUNTIMES, agent_skill_dirs
from teatree.cli.setup.clone import find_main_clone
from teatree.cli.setup.command import setup_app

# ``AGENT_SKILL_RUNTIMES`` / ``agent_skill_dirs`` are re-exported so external
# callers and tests see a single import path for these setup-adjacent knobs;
# their canonical definition lives in ``doctor`` to keep ``setup → doctor``
# imports one-directional. ``find_main_clone`` is re-exported for ``t3 update``.
__all__ = ["AGENT_SKILL_RUNTIMES", "agent_skill_dirs", "find_main_clone", "setup_app"]
