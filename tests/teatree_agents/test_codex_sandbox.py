from pathlib import Path

import pytest

from teatree.agents import codex_sandbox
from teatree.agents.codex_sandbox import CONTAINER_IS_SANDBOX_ENV
from teatree.utils.ports import running_in_container


@pytest.mark.parametrize(
    ("marker", "role", "opt_in", "expected"),
    [
        (False, None, None, False),
        (False, "worker", "1", False),
        (True, "worker", None, False),
        (True, "admin", None, False),
        (True, None, "1", True),
        (True, "worker", "1", True),
        (True, "worker", "0", False),
        (True, "worker", "true", False),
        (True, "worker", "", False),
    ],
    ids=[
        "host",
        "host-with-the-opt-in-leaked",
        "core-compose-worker",
        "admin",
        "opt-in",
        "opted-in-worker",
        "opt-in-off",
        "opt-in-not-one",
        "opt-in-empty",
    ],
)
def test_container_is_the_sandbox_only_on_an_explicit_opt_in_inside_a_container(
    monkeypatch: pytest.MonkeyPatch, *, marker: bool, role: str | None, opt_in: str | None, expected: bool
) -> None:
    monkeypatch.setattr(codex_sandbox, "running_in_container", running_in_container)
    monkeypatch.setattr(Path, "exists", lambda _path: marker)
    monkeypatch.setattr(Path, "read_text", lambda _path, **_kwargs: "0::/user.slice\n")
    for name, value in (("TEATREE_ROLE", role), (CONTAINER_IS_SANDBOX_ENV, opt_in)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    codex_sandbox.container_is_the_sandbox.cache_clear()
    try:
        assert codex_sandbox.container_is_the_sandbox() is expected
    finally:
        codex_sandbox.container_is_the_sandbox.cache_clear()
