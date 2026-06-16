"""Shared helpers to set ``on_behalf_post_mode = "immediate"`` for transport tests.

The tri-state ``on_behalf_post_mode`` gate (#960) defaults to
``DRAFT_OR_ASK`` globally and is enforced at the ``_BaseReplier``
chokepoint. Pre-existing tests that exercise transport *mechanics*
(idempotency, status recording, backend wiring) — not the gate, which
has its own dedicated suites — need IMMEDIATE mode (gate off) so their
assertions still hold.

Under the #1775 DB/TOML hard partition ``on_behalf_post_mode`` is
DB-home: a ``[teatree]`` TOML value for it is IGNORED on read. These
helpers therefore set the mode through the ``T3_ON_BEHALF_POST_MODE``
env var — the highest-precedence tier, which wins for a DB-home key and
needs no database, so the helper works for every caller (DB-backed or
not) without a ``ConfigSetting`` write. They still repoint
``teatree.config.CONFIG_PATH`` at an empty TOML so the developer's real
``~/.teatree.toml`` is never read.

Two flavours are exported because the consumers differ:

*   :func:`mode_immediate_cm` — a ``contextlib`` context manager (works
    for both ``unittest.TestCase`` classes and pytest functions that
    prefer ``with mode_immediate_cm(): ...``).
*   :func:`disable_on_behalf_gate` — a one-shot helper that sets the env
    var for the lifetime of the test using pytest's ``monkeypatch``
    fixture, so an autouse fixture can call it without context-manager
    scoping.

The legacy names ``on_behalf_gate_off`` and ``_GATE_OFF_TOML`` remain as
deprecated aliases pointing at the new helpers so existing import sites
keep working through the deprecation window.
"""

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

_MODE_IMMEDIATE_TOML = "[teatree]\n"
_MODE_IMMEDIATE_ENV = "immediate"


@contextmanager
def mode_immediate_cm() -> Iterator[None]:
    """Context manager: ``on_behalf_post_mode`` is IMMEDIATE inside the block."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / ".teatree.toml"
        cfg.write_text(_MODE_IMMEDIATE_TOML, encoding="utf-8")
        with (
            patch("teatree.config.CONFIG_PATH", cfg),
            patch.dict(os.environ, {"T3_ON_BEHALF_POST_MODE": _MODE_IMMEDIATE_ENV}),
        ):
            yield


def disable_on_behalf_gate(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set ``on_behalf_post_mode`` to IMMEDIATE (DB-home, via env) for this test.

    Designed for autouse fixtures that disable the gate for the lifetime
    of a class/function-scoped test: ``monkeypatch`` reverts both the env
    var and ``CONFIG_PATH`` at teardown so the global gate-on default is
    restored automatically.
    """
    cfg = tmp_path_factory.mktemp("on_behalf_gate") / ".teatree.toml"
    cfg.write_text(_MODE_IMMEDIATE_TOML, encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", _MODE_IMMEDIATE_ENV)


# Deprecated legacy aliases — kept so existing import sites keep working.
_GATE_OFF_TOML = _MODE_IMMEDIATE_TOML
on_behalf_gate_off = mode_immediate_cm
