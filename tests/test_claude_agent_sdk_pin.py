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

import importlib.metadata
import tomllib
from pathlib import Path
from typing import Any

import yaml
from packaging.requirements import Requirement

from teatree.eval.discovery import discover_specs
from teatree.eval.surface import is_advisory, mislabelled_interactive_specs

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_LOCK = _REPO_ROOT / "uv.lock"
_DEPENDABOT = _REPO_ROOT / ".github" / "dependabot.yml"

_PINNED_VERSION = "0.2.95"
_PACKAGE = "claude-agent-sdk"

#: The dependency whose major the pin's ceiling is set by. ``teatree.mcp`` imports the
#: 2.0 module layout (``mcp.server.mcpserver.MCPServer``) across eight modules.
_MCP = "mcp"


def _sdk_constraint() -> str:
    deps = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    matches = [d for d in deps if d.replace(" ", "").startswith(_PACKAGE)]
    assert len(matches) == 1, f"expected exactly one {_PACKAGE} dependency, got {matches}"
    return matches[0].replace(" ", "")


def _project_requirement(name: str) -> Requirement:
    deps = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    matches = [Requirement(d) for d in deps]
    hits = [r for r in matches if r.name == name]
    assert len(hits) == 1, f"expected exactly one {name} dependency, got {hits}"
    return hits[0]


def _sdk_requirement_on(name: str) -> Requirement:
    """The pinned SDK's OWN declared requirement on *name*, read from its metadata.

    ``uv.lock`` records a resolved dependency graph with no specifiers, so the wheel's
    own bound is only readable from the installed distribution.
    """
    installed = importlib.metadata.version(_PACKAGE)
    assert installed == _PINNED_VERSION, (
        f"the environment has {_PACKAGE}=={installed} but the pin is {_PINNED_VERSION}; "
        "run `uv sync` so this reads the pinned wheel's own metadata."
    )
    requirements = [Requirement(r) for r in importlib.metadata.requires(_PACKAGE) or []]
    hits = [r for r in requirements if r.name == name and not r.marker]
    assert len(hits) == 1, f"expected exactly one unconditional {name} requirement, got {hits}"
    return hits[0]


def _locked_version_of(package: str) -> str:
    lock = tomllib.loads(_LOCK.read_text(encoding="utf-8"))
    matches = [p["version"] for p in lock["package"] if p["name"] == package]
    assert len(matches) == 1, f"expected exactly one locked {package}, got {matches}"
    return matches[0]


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


class TestThePinsCeilingIsTheMcpMajor:
    """The pin has a CEILING, and it is ``mcp``'s major — not a matter of taste.

    ``claude-agent-sdk`` 0.2.96 added an ``mcp<2.0.0`` cap, while ``teatree.mcp``
    imports the 2.0 layout (``mcp.server.mcpserver.MCPServer``) under the project's
    ``mcp>=2,<3`` bound. The two are strictly disjoint, so the combination does not
    resolve AT ALL: ``uv sync`` fails before a single test runs and every downstream
    lane reds with no stated cause. These name the cause where a bump is made, so
    raising the pin past the ceiling reds here on the reason rather than as an opaque
    resolver error. Lifting the ceiling means the SDK dropping its cap — never moving
    ``mcp`` back to 1.x, which the module layout above cannot survive.
    """

    def test_the_locked_mcp_satisfies_the_pinned_sdks_own_bound(self) -> None:
        requirement = _sdk_requirement_on(_MCP)
        locked = _locked_version_of(_MCP)
        assert requirement.specifier.contains(locked), (
            f"{_PACKAGE}=={_PINNED_VERSION} requires {requirement}, which excludes the locked "
            f"{_MCP}=={locked}. This SDK release cannot be used while teatree imports the "
            f"{_MCP} 2.0 module layout; pin the SDK back below its {_MCP} cap."
        )

    def test_the_locked_mcp_satisfies_the_projects_own_bound(self) -> None:
        requirement = _project_requirement(_MCP)
        locked = _locked_version_of(_MCP)
        assert requirement.specifier.contains(locked), (
            f"the lock resolved {_MCP}=={locked}, outside the project's own {requirement}. "
            f"`teatree.mcp.server` imports `{_MCP}.server.mcpserver.MCPServer`, so a resolution "
            "that drops below the bound crashes the MCP server on import."
        )


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
