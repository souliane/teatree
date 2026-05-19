"""Shared helpers to set ``on_behalf_post_mode = "immediate"`` for transport tests.

The tri-state ``on_behalf_post_mode`` gate (#960) defaults to
``DRAFT_OR_ASK`` globally and is enforced at the ``_BaseReplier``
chokepoint. Pre-existing tests that exercise transport *mechanics*
(idempotency, status recording, backend wiring) — not the gate, which
has its own dedicated suites — need IMMEDIATE mode (gate off) so their
assertions still hold.

Two flavours are exported because the consumers differ:

*   :func:`mode_immediate_cm` — a ``contextlib`` context manager (works
    for both ``unittest.TestCase`` classes and pytest functions that
    prefer ``with mode_immediate_cm(): ...``).
*   :func:`disable_on_behalf_gate` — a one-shot helper that points
    ``teatree.config.CONFIG_PATH`` at an immediate-mode TOML file for the
    lifetime of the test using pytest's ``tmp_path_factory`` +
    ``monkeypatch`` fixtures, so an autouse fixture can call it without
    context-manager scoping.

Both write the same minimal TOML
(``[teatree] on_behalf_post_mode = "immediate"``) and only touch
``teatree.config.CONFIG_PATH``; everything else is left untouched.

The legacy names ``on_behalf_gate_off`` and ``_GATE_OFF_TOML`` remain as
deprecated aliases pointing at the new helpers so existing import sites
keep working through the deprecation window.
"""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

_MODE_IMMEDIATE_TOML = '[teatree]\non_behalf_post_mode = "immediate"\n'


@contextmanager
def mode_immediate_cm() -> Iterator[None]:
    """Context manager: ``on_behalf_post_mode`` is IMMEDIATE inside the block."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / ".teatree.toml"
        cfg.write_text(_MODE_IMMEDIATE_TOML, encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", cfg):
            yield


def disable_on_behalf_gate(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point ``teatree.config.CONFIG_PATH`` at an immediate-mode TOML for this test.

    Designed for autouse fixtures that disable the gate for the lifetime
    of a class/function-scoped test: ``monkeypatch`` is reverted by pytest
    at teardown so the global gate-on default is restored automatically.
    """
    cfg = tmp_path_factory.mktemp("on_behalf_gate") / ".teatree.toml"
    cfg.write_text(_MODE_IMMEDIATE_TOML, encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


# Deprecated legacy aliases — kept so existing import sites keep working.
_GATE_OFF_TOML = _MODE_IMMEDIATE_TOML
on_behalf_gate_off = mode_immediate_cm
