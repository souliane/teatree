"""Shared helpers to disable the on-behalf gate for transport-mechanics tests.

The ``ask_before_post_on_behalf`` gate (#960) defaults ON globally and is
enforced at the ``_BaseReplier`` chokepoint. Pre-existing tests that
exercise transport *mechanics* (idempotency, status recording, backend
wiring) — not the gate, which has its own dedicated suites — need the gate
OFF so their assertions still hold.

Two flavours are exported because the consumers differ:

* :func:`on_behalf_gate_off` — a ``contextlib`` context manager (works for
    both ``unittest.TestCase`` classes and pytest functions that prefer
    ``with on_behalf_gate_off(): ...``).
* :func:`disable_on_behalf_gate` — a one-shot helper that points
    ``teatree.config.CONFIG_PATH`` at a gate-OFF TOML file for the lifetime
    of the test using pytest's ``tmp_path_factory`` + ``monkeypatch``
    fixtures, so an autouse fixture can call it without context-manager
    scoping.

Both write the same minimal TOML (``[teatree] ask_before_post_on_behalf =
false``) and only touch ``teatree.config.CONFIG_PATH``; everything else is
left untouched.
"""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

_GATE_OFF_TOML = "[teatree]\nask_before_post_on_behalf = false\n"


@contextmanager
def on_behalf_gate_off() -> Iterator[None]:
    """Context manager: ``ask_before_post_on_behalf`` is OFF inside the block."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / ".teatree.toml"
        cfg.write_text(_GATE_OFF_TOML, encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", cfg):
            yield


def disable_on_behalf_gate(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point ``teatree.config.CONFIG_PATH`` at a gate-OFF TOML for this test.

    Designed for autouse fixtures that disable the gate for the lifetime
    of a class/function-scoped test: ``monkeypatch`` is reverted by pytest
    at teardown so the global gate-on default is restored automatically.
    """
    cfg = tmp_path_factory.mktemp("on_behalf_gate") / ".teatree.toml"
    cfg.write_text(_GATE_OFF_TOML, encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
