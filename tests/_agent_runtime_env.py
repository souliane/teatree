"""Shared test-infra helper: pin ``agent_runtime`` through its env seam (#3895).

The shipped default is ``headless``, so a test ABOUT the in-session interactive
lane — the ``/loop`` slot's claim filter, ``execution_target`` at INSERT time,
the interactive-followup path — must say so rather than inherit the ambient
default. A test that reads the ambient value is not testing the lane it names;
it is testing whatever the shipped file happens to say that week.

``T3_AGENT_RUNTIME`` is the resolver's own documented override seam
(``setting_registries.ENV_SETTING_OVERRIDES``) and wins over every DB row, so
driving it pins the runtime while replacing no module attribute — the same
reason ``tests/_loop_principal_env`` drives env rather than ``mock.patch``
(``tests/CLAUDE.md``: a patch leaks into any module whose first import happens
while it is live).
"""

from collections.abc import Iterator
from contextlib import contextmanager

from teatree.config import AgentRuntime
from teatree.utils.env import patched_environ

#: The one env var ``agent_runtime`` resolves from before any DB row.
AGENT_RUNTIME_ENV = "T3_AGENT_RUNTIME"


@contextmanager
def pinned_agent_runtime(runtime: AgentRuntime) -> Iterator[None]:
    """Resolve ``agent_runtime`` to *runtime* for the duration of the block."""
    with patched_environ({AGENT_RUNTIME_ENV: runtime.value}):
        yield


@contextmanager
def interactive_runtime() -> Iterator[None]:
    """Pin the in-session INTERACTIVE lane — for a test that is about that lane."""
    with pinned_agent_runtime(AgentRuntime.INTERACTIVE):
        yield
