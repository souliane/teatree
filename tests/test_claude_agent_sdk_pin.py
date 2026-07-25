"""Guards the exact pin of ``claude-agent-sdk`` (souliane/teatree#3125).

A ``>=`` floor let the installed ``t3`` env drift from the known-green
``0.2.94`` to ``0.2.113`` (with claude CLI 2.1.204), which emits a markdown
``**AskUserQuestion**`` chip instead of a ``tool_use`` block — breaking the eval
AskUserQuestion flow and the question-drain (``teatree.eval.message_mapping``
only maps a ``ToolUseBlock`` to a ``tool_use`` event; a text chip is never
mapped). The constraint must stay an EXACT pin so any bump is deliberate.

An exact pin alone proved NOT to be enough: an automated dependency bump
rewrote the pin AND this module's ``_PINNED_VERSION`` in the same change, so the
guard passed while the quarantine it existed to hold was lifted. The pin is
therefore paired with a Dependabot ``ignore`` entry — the only mechanism that
stops the bump from being PROPOSED — and
:meth:`TestClaudeAgentSdkQuarantine.test_dependabot_cannot_propose_an_sdk_bump`
asserts that entry stays in place.
"""

import tomllib
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_LOCK = _REPO_ROOT / "uv.lock"
_DEPENDABOT = _REPO_ROOT / ".github" / "dependabot.yml"

_PINNED_VERSION = "0.2.94"
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


def _pip_update_entries() -> list[dict[str, Any]]:
    config = yaml.safe_load(_DEPENDABOT.read_text(encoding="utf-8"))
    return [entry for entry in config["updates"] if entry.get("package-ecosystem") == "pip"]


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


class TestClaudeAgentSdkQuarantine:
    """The pin is a QUARANTINE, so no automated bump may propose lifting it."""

    def test_dependabot_cannot_propose_an_sdk_bump(self) -> None:
        pip_entries = _pip_update_entries()
        assert pip_entries, "expected a pip package-ecosystem entry in .github/dependabot.yml"
        ignored = {ignore.get("dependency-name") for entry in pip_entries for ignore in entry.get("ignore", [])}
        assert _PACKAGE in ignored, (
            f"{_PACKAGE} must carry a Dependabot `ignore` entry: the exact pin alone did "
            "not hold the quarantine — an automated bump rewrote the pin AND this module's "
            "_PINNED_VERSION together, so the guard passed while the known-green build was "
            "abandoned and every AskUserQuestion eval scenario went red. A bump must be "
            "opened by hand and re-verified against the metered eval lane."
        )
