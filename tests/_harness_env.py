"""Shared test-infra helper: pin the agent-harness signature this process carries.

A suite run from inside a headless agent inherits that agent's signature, so a test
about "an answer from a terminal" must clear it rather than assume a clean env.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from teatree.utils.env import patched_environ

HEADLESS_AGENT_ENV: Mapping[str, str] = {"CLAUDE_AGENT_SDK_VERSION": "0.2.161", "CLAUDE_CODE_ENTRYPOINT": "sdk-py"}


@contextmanager
def harness_signature(env: Mapping[str, str]) -> Iterator[None]:
    """Carry exactly *env*'s harness signature for the block; an empty *env* is a plain terminal."""
    with patched_environ(env, remove=tuple(name for name in HEADLESS_AGENT_ENV if name not in env)):
        yield
