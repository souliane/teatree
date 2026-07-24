"""Guards the exact pin of ``claude-agent-sdk`` (souliane/teatree#3125).

A ``>=`` floor let the installed ``t3`` env drift from the known-green
``0.2.94`` to ``0.2.113`` (with claude CLI 2.1.204), which emits a markdown
``**AskUserQuestion**`` chip instead of a ``tool_use`` block — breaking the eval
AskUserQuestion flow and the question-drain (``teatree.eval.message_mapping``
only maps a ``ToolUseBlock`` to a ``tool_use`` event; a text chip is never
mapped). The constraint must stay an EXACT pin so any bump is deliberate.
"""

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_LOCK = _REPO_ROOT / "uv.lock"

_PINNED_VERSION = "0.2.125"
_PACKAGE = "claude-agent-sdk"


def _sdk_constraint() -> str:
    deps = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    matches = [d for d in deps if d.replace(" ", "").startswith(_PACKAGE)]
    assert len(matches) == 1, f"expected exactly one {_PACKAGE} dependency, got {matches}"
    return matches[0].replace(" ", "")


def _locked_version() -> str:
    lock = tomllib.loads(_LOCK.read_text(encoding="utf-8"))
    matches = [p["version"] for p in lock["package"] if p["name"] == _PACKAGE]
    assert len(matches) == 1, f"expected exactly one locked {_PACKAGE}, got {matches}"
    return matches[0]


class TestClaudeAgentSdkPin:
    def test_pyproject_pins_sdk_exactly_not_a_floor(self) -> None:
        constraint = _sdk_constraint()
        assert constraint == f"{_PACKAGE}=={_PINNED_VERSION}", (
            f"claude-agent-sdk must be an EXACT pin ({_PACKAGE}=={_PINNED_VERSION}), "
            f"never a >= floor — a floor let the env drift to the 0.2.113 chip "
            f"regression (#3125). Got: {constraint!r}"
        )

    def test_lock_resolves_sdk_to_pinned_version(self) -> None:
        assert _locked_version() == _PINNED_VERSION
