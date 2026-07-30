"""Guards the exact pin of ``claude-agent-sdk`` and the guard that REPLACED its quarantine.

The constraint stays an EXACT pin, for a reason unrelated to the quarantine:
``tests/test_claude_cli_pin.py`` derives the eval/test image's ``claude`` CLI generation from
this exact version (the CLI the wheel bundles and actually executes), and reds until that
generation is re-derived from the new wheel. A ``>=`` floor makes that derivation impossible.

The pin is no longer a QUARANTINE. It used to carry a Dependabot ``ignore`` entry because a
bundled claude CLI at/after 2.1.204 renders an ``AskUserQuestion`` call as a markdown chip
instead of a ``tool_use`` block (``teatree.eval.message_mapping`` maps only a
``ToolUseBlock``), so every scenario matching that tool call went red. Freezing a whole
dependency to protect those scenarios defended the wrong asset — the contract teatree owns is
the headless Slack round trip, not one CLI's rendering (souliane/teatree#3855).

The quarantine is therefore REPLACED, not dropped: every scenario that hard-requires the
interactive tool call now carries ``surface: interactive`` and is advisory.
:class:`TestTheGuardThatReplacedTheQuarantine` asserts that replacement is load-bearing —
it is what earns Dependabot the right to watch the package again.
"""
# test-path: cross-cutting — a dependency-manifest contract test that also reads the eval
# catalog, because the manifest's `ignore` entry and the catalog's labelling are one decision.

import tomllib
from pathlib import Path
from typing import Any

import yaml

from teatree.eval.discovery import discover_specs
from teatree.eval.surface import is_advisory, mislabelled_interactive_specs

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
            f"claude-agent-sdk must be an EXACT pin ({_PACKAGE}=={_PINNED_VERSION}), never a >= "
            "floor: tests/test_claude_cli_pin.py derives the eval/test image's claude CLI "
            "generation from this exact version, and a floor makes that derivation impossible. "
            f"Got: {constraint!r}"
        )

    def test_lock_resolves_sdk_to_pinned_version(self) -> None:
        assert _locked_version() == _PINNED_VERSION


class TestTheGuardThatReplacedTheQuarantine:
    """Dependabot watches the SDK again — but only because the advisory label holds."""

    def test_dependabot_watches_the_sdk_again(self) -> None:
        pip_entries = _pip_update_entries()
        assert pip_entries, "expected a pip package-ecosystem entry in .github/dependabot.yml"
        ignored = {ignore.get("dependency-name") for entry in pip_entries for ignore in entry.get("ignore", [])}
        assert _PACKAGE not in ignored, (
            f"{_PACKAGE} must NOT carry a Dependabot `ignore` entry. The quarantine existed to stop "
            "a bundled-CLI rendering change from reddening the AskUserQuestion scenarios; those are "
            "advisory now (`surface: interactive`), so freezing the dependency defends nothing and "
            "only holds the SDK stale."
        )

    def test_the_replacement_guard_is_load_bearing(self) -> None:
        # Lifting the ignore entry is only safe while the advisory labelling is enforced. If the
        # catalog gate is gone or vacuous, the quarantine has no successor and the ignore entry
        # should come back — so this asserts the successor exists and has scenarios to hold.
        specs = discover_specs()
        assert not mislabelled_interactive_specs(specs), (
            "a scenario hard-requires the AskUserQuestion tool call while still gating — the guard "
            "that replaced the quarantine is not holding"
        )
        assert [spec for spec in specs if is_advisory(spec)], (
            "no scenario carries `surface: interactive`, so nothing is actually protected by the "
            "advisory label that replaced the quarantine"
        )
