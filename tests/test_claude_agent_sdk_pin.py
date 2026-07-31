"""Guards the ``claude-agent-sdk`` pin, the uv override it needs, and its ex-quarantine.

The constraint stays an EXACT pin, for a reason unrelated to the quarantine:
``tests/test_claude_cli_pin.py`` derives the eval/test image's ``claude`` CLI generation from
this exact version (the CLI the wheel bundles and actually executes), and reds until that
generation is re-derived from the new wheel. A ``>=`` floor makes that derivation impossible.

The SDK declares ``mcp<2.0.0`` while teatree declares ``mcp>=2,<3``, so the pin resolves only
behind the ``[tool.uv] override-dependencies`` entry in ``pyproject.toml``.
:class:`TestTheOverrideIsHonest` is what earns that override the right to exist: it re-derives
the SDK's ``mcp`` imports from the INSTALLED wheel on every run and asserts each module and
symbol resolves under the installed ``mcp``. Without it the override would be a standing
unverified assumption, and a future SDK release reaching for an mcp 1.x-only symbol would
fail at runtime rather than in CI.

The pin is not a QUARANTINE. It used to carry a Dependabot ``ignore`` entry because a
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

import ast
import importlib
import importlib.metadata
import importlib.util
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

_PINNED_VERSION = "0.2.128"
_PACKAGE = "claude-agent-sdk"
_SDK_MODULE = "claude_agent_sdk"

#: The dependency the SDK's declared bound and teatree's own disagree on. ``teatree.mcp``
#: imports the 2.0 module layout (``mcp.server.mcpserver.MCPServer``) across eight modules.
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


def _override_requirements() -> list[Requirement]:
    raw = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return [Requirement(spec) for spec in raw.get("tool", {}).get("uv", {}).get("override-dependencies", [])]


def _installed_sdk_sources() -> list[Path]:
    spec = importlib.util.find_spec(_SDK_MODULE)
    assert spec is not None, f"{_SDK_MODULE} is not installed"
    roots = spec.submodule_search_locations
    assert roots, f"{_SDK_MODULE} is installed but exposes no package directory to walk"
    return sorted(path for root in roots for path in Path(root).rglob("*.py"))


def _sdk_mcp_imports() -> dict[str, set[str]]:
    """``mcp`` module path -> the names the INSTALLED SDK imports from it.

    Derived from the wheel on disk rather than a hand-written list, so a release that
    reaches for a new symbol is seen the moment it is installed. ``TYPE_CHECKING`` blocks
    are walked too: a name resolvable only at type-check time is still a real coupling, and
    ``teatree.eval`` re-exports SDK types into annotations that pydantic resolves at runtime.
    """
    imports: dict[str, set[str]] = {}
    for source in _installed_sdk_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == _MCP or alias.name.startswith(f"{_MCP}."):
                        imports.setdefault(alias.name, set())
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module
                and (node.module == _MCP or node.module.startswith(f"{_MCP}."))
            ):
                imports.setdefault(node.module, set()).update(alias.name for alias in node.names)
    return imports


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


class TestTheOverrideIsHonest:
    """The ``mcp`` bound the SDK declares is overridden; these prove nothing real is masked.

    ``claude-agent-sdk`` declares ``mcp<2.0.0`` while ``teatree.mcp`` imports the 2.0 layout
    (``mcp.server.mcpserver.MCPServer``) under the project's ``mcp>=2,<3``. Left alone the two
    are disjoint and ``uv sync`` fails before a single test runs, so ``pyproject.toml``'s
    ``[tool.uv] override-dependencies`` replaces the SDK's bound with the project's own.

    That is safe only because the SDK's cap is broader than its usage: it reaches only the
    LOW-LEVEL ``mcp.server.Server`` and ``mcp.types`` surface, which mcp 2.x still provides,
    and never the ``MCPServer`` surface teatree uses. An override that is wrong, though, is
    invisible — the resolver stops complaining and the break moves to runtime. So the import
    set is RE-DERIVED from the installed wheel here rather than trusted, and every module and
    symbol in it must resolve under the installed ``mcp``.
    """

    def test_pyproject_overrides_the_mcp_bound_to_the_projects_own(self) -> None:
        overrides = {requirement.name: requirement for requirement in _override_requirements()}
        assert _MCP in overrides, (
            f"the SDK pin resolves only while `[tool.uv] override-dependencies` replaces its "
            f"{_MCP} bound; without the entry `uv lock` fails with an opaque resolver error."
        )
        assert str(overrides[_MCP].specifier) == str(_project_requirement(_MCP).specifier), (
            f"the {_MCP} override must restate the project's OWN bound and nothing wider — a "
            "wider override would let the resolver pick a major the suite never runs against. "
            f"Override: {overrides[_MCP]}; project: {_project_requirement(_MCP)}."
        )

    def test_the_override_still_has_something_to_override(self) -> None:
        # When a release finally declares a bound the project's own already satisfies, the
        # override is dead weight that would silently outlive its reason — so it reds here and
        # gets deleted rather than accumulating.
        declared = _sdk_requirement_on(_MCP)
        locked = _locked_version_of(_MCP)
        assert not declared.specifier.contains(locked), (
            f"{_PACKAGE}=={_PINNED_VERSION} declares {declared}, which already admits the locked "
            f"{_MCP}=={locked}. The `[tool.uv] override-dependencies` entry for {_MCP} is no "
            "longer doing anything — remove it, and this test with it."
        )

    def test_every_mcp_module_the_sdk_imports_resolves(self) -> None:
        missing = sorted(module for module in _sdk_mcp_imports() if importlib.util.find_spec(module) is None)
        assert not missing, (
            f"{_PACKAGE}=={_PINNED_VERSION} imports {_MCP} modules the installed "
            f"{_MCP}=={_locked_version_of(_MCP)} does not provide: {missing}. The override is "
            f"masking a real incompatibility — the SDK's declared {_MCP} bound is now telling "
            "the truth about its usage."
        )

    def test_every_mcp_symbol_the_sdk_imports_resolves(self) -> None:
        unresolved: list[str] = []
        for module_path, names in sorted(_sdk_mcp_imports().items()):
            if importlib.util.find_spec(module_path) is None:
                continue  # reported by the module-level assertion above
            module = importlib.import_module(module_path)
            unresolved.extend(f"{module_path}.{name}" for name in sorted(names) if not hasattr(module, name))
        assert not unresolved, (
            f"{_PACKAGE}=={_PINNED_VERSION} imports names the installed "
            f"{_MCP}=={_locked_version_of(_MCP)} does not define: {unresolved}. The override is "
            "masking a real incompatibility, not a bound that is merely broader than its usage."
        )

    def test_the_walk_sees_the_surface_that_justifies_the_override(self) -> None:
        # The control: a walk that found nothing would pass both assertions above. The override
        # rests on the claim that the SDK touches only the LOW-LEVEL server surface, so the walk
        # must actually see that surface — and must NOT see the one teatree migrated.
        found = _sdk_mcp_imports()
        assert "Server" in found.get("mcp.server", set())
        assert "ToolAnnotations" in found.get("mcp.types", set())
        assert "mcp.server.mcpserver" not in found, (
            "the SDK now imports the same surface as `teatree.mcp.server`; the two no longer "
            "sit on disjoint APIs and the override's justification needs re-deriving."
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
