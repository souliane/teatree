"""Guards the npm-installed Claude CLI pin, per tier (souliane/teatree#3748).

``claude-agent-sdk`` is held at an exact pin with a guard test
(``tests/test_claude_agent_sdk_pin.py``). The Claude CLI installed via
``npm install -g`` has no manifest at all: Dependabot cannot see a
non-manifest-driven install, so without these tests each build would resolve to
whatever ``latest`` happened to be that day.

The pin is deliberately split into TWO TIERS, because the two consumers are
pinned to different things and one global version would break whichever tier lost:

**eval/test images** pin the generation the pinned SDK BUNDLES. The eval runner
never executes the global binary — ``SubprocessCLITransport._find_cli()`` returns
``_find_bundled_cli()`` first, and ``shutil.which("claude")`` in
``teatree.eval.api_runner`` is only a provisioning presence-gate. Matching the
bundle keeps the two CLIs in one image from disagreeing.

**The deployed runtime image** pins a current known-good version, chosen
independently. That image feeds the paths that DO exec the global binary
(``teatree.cli.loop.app``'s ``os.execv``, ``teatree.cli.agent``,
``teatree.agents.web_terminal``, ``teatree.core.management.commands.tasks``), so it
may LEAD the bundle but never trail it: a generation behind the introduction of
``claude-opus-5`` breaks ``teatree.agents.model_tiering``'s ``TIER_MODELS``, which
sets that model as the ``frontier`` tier.

So these tests assert agreement PER TIER and never one global version across all
sites — that global equality is precisely the mistake the split exists to avoid.
The two tiers may happen to name the same version when the SDK's bundle catches up
with the runtime's choice; that coincidence is not a coupling, and either constant
moves on its own.
"""

import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SELF = Path(__file__).resolve()
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_RUNTIME_DOCKERFILE = _REPO_ROOT / "deploy" / "Dockerfile"

#: The SDK pin whose bundled CLI the eval/test tier tracks. When the SDK pin moves,
#: this constant reds and :data:`_SDK_BUNDLED_CLI_VERSION` must be re-derived from
#: the NEW wheel's ``claude_agent_sdk/_bundled/claude --version`` — never assumed.
_PINNED_SDK_VERSION = "0.2.134"

#: ``claude_agent_sdk/_bundled/claude --version`` from the wheel of
#: :data:`_PINNED_SDK_VERSION` → ``2.1.226 (Claude Code)``.
_SDK_BUNDLED_CLI_VERSION = "2.1.226"

#: The deployed runtime's pin: the version the factory host runs today.
_RUNTIME_CLI_VERSION = "2.1.226"

#: ``pyright-langserver`` for the pyright-lsp plugin in the runtime image.
_PYRIGHT_VERSION = "1.1.411"

_EVAL_TEST_SITES = frozenset(
    {
        "dev/Dockerfile.test",
        ".gitlab-ci.yml",
        ".github/workflows/eval.yml",
        ".github/workflows/eval-pr.yml",
        ".github/workflows/eval-pr-reusable.yml",
        ".github/workflows/eval-nightly.yml",
        ".github/workflows/eval-weekly-reusable.yml",
        ".github/workflows/eval-ci-heal.yml",
    }
)
_RUNTIME_SITES = frozenset({"deploy/Dockerfile"})

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        ".ruff_cache",
        ".pytest_cache",
        ".mypy_cache",
        "__pycache__",
        "node_modules",
        "dist",
        "mutants",
        "staticfiles",
        "htmlcov",
    }
)

_INSTALL_PATTERN = re.compile(r"npm install -g [^\n]*?@anthropic-ai/claude-code(?:@(?P<version>[0-9][^\s\\'\"]*))?")
_PYRIGHT_PATTERN = re.compile(r"npm install -g [^\n]*?\bpyright(?:@(?P<version>[0-9][^\s\\'\"]*))?")


def _is_skipped_dir(name: str) -> bool:
    """Whether the walk descends into *name*.

    Virtualenvs are matched by PREFIX, not by an exact name. A venv holds installed
    third-party code, so a pinned CLI found inside one is the SDK's own vendored copy
    rather than a site this repo controls — and the repo creates more than one venv, so
    enumerating each exact name means the scan breaks again the next time one is added
    under a new name.
    """
    return name.startswith(".venv") or name in _SKIP_DIRS


def _repo_text_files() -> Iterator[Path]:
    stack = [_REPO_ROOT]
    while stack:
        for entry in sorted(stack.pop().iterdir()):
            if entry.is_dir():
                if not _is_skipped_dir(entry.name):
                    stack.append(entry)
            elif entry.is_file() and entry.resolve() != _SELF:
                yield entry


def _install_sites() -> dict[str, str | None]:
    """Repo-relative path → the pinned version, or ``None`` when unpinned."""
    sites: dict[str, str | None] = {}
    for path in _repo_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in _INSTALL_PATTERN.finditer(text):
            sites[path.relative_to(_REPO_ROOT).as_posix()] = match.group("version")
    return sites


def _sdk_pin() -> str:
    deps = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    matches = [d.replace(" ", "") for d in deps if d.replace(" ", "").startswith("claude-agent-sdk")]
    assert len(matches) == 1, f"expected exactly one claude-agent-sdk dependency, got {matches}"
    return matches[0].removeprefix("claude-agent-sdk==")


def _as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


class TestEveryInstallSiteIsPinned:
    def test_no_install_resolves_to_whatever_latest_is_today(self) -> None:
        unpinned = sorted(path for path, version in _install_sites().items() if version is None)
        assert not unpinned, (
            "every `npm install -g @anthropic-ai/claude-code` must carry an `@<version>` "
            "suffix — a bare install resolves to whatever `latest` is on build day, which "
            "no lockfile, guard, or bot can see. Unpinned: " + ", ".join(unpinned)
        )

    def test_every_site_is_classified_into_a_tier(self) -> None:
        # A new workflow that copies an existing install step lands here unclassified,
        # so it cannot silently inherit the wrong tier's version.
        discovered = set(_install_sites())
        known = _EVAL_TEST_SITES | _RUNTIME_SITES
        assert discovered == known, (
            "every Claude CLI install site must be classified as eval/test (pins the SDK-bundled "
            f"generation) or runtime (pins a current known-good version). New: {sorted(discovered - known)}; "
            f"gone: {sorted(known - discovered)}."
        )


class TestTheEvalTestTierTracksTheSdkBundle:
    def test_every_eval_test_site_pins_the_bundled_generation(self) -> None:
        sites = _install_sites()
        disagreeing = {
            path: sites.get(path) for path in sorted(_EVAL_TEST_SITES) if sites.get(path) != _SDK_BUNDLED_CLI_VERSION
        }
        assert not disagreeing, (
            f"every eval/test image must install claude-code@{_SDK_BUNDLED_CLI_VERSION} — the generation "
            f"`claude-agent-sdk=={_PINNED_SDK_VERSION}` bundles and actually executes — so the global binary "
            f"and the bundle in one image cannot disagree. Got: {disagreeing}"
        )

    def test_the_sdk_pin_the_tier_tracks_has_not_moved(self) -> None:
        assert _sdk_pin() == _PINNED_SDK_VERSION, (
            f"the eval/test tier pins the CLI generation bundled by claude-agent-sdk=={_PINNED_SDK_VERSION}. "
            f"The SDK pin is now {_sdk_pin()!r}, so its bundled CLI version must be re-derived by running "
            f"`claude_agent_sdk/_bundled/claude --version` from the NEW wheel, and every eval/test site "
            "re-pinned to it."
        )


class TestTheRuntimeTierIsPinnedIndependently:
    def test_every_runtime_site_pins_one_current_known_good_version(self) -> None:
        sites = _install_sites()
        disagreeing = {
            path: sites.get(path) for path in sorted(_RUNTIME_SITES) if sites.get(path) != _RUNTIME_CLI_VERSION
        }
        assert not disagreeing, (
            f"the deployed runtime image must install claude-code@{_RUNTIME_CLI_VERSION}: it execs the GLOBAL "
            "binary (t3 loop start's os.execv, the agent command, the ttyd web terminal, the tasks command). "
            f"Got: {disagreeing}"
        )

    def test_the_runtime_is_never_rolled_back_behind_the_eval_bundle(self) -> None:
        # The tiers are pinned separately, but only in one direction: the deployed CLI
        # may lead the bundle, never trail it. Pinning the runtime back to the bundled
        # generation would take it behind `claude-opus-5`, which model_tiering's
        # TIER_MODELS resolves as the `frontier` tier, breaking model selection in
        # production.
        assert _as_tuple(_RUNTIME_CLI_VERSION) >= _as_tuple(_SDK_BUNDLED_CLI_VERSION), (
            f"the runtime pin {_RUNTIME_CLI_VERSION} trails the SDK-bundled {_SDK_BUNDLED_CLI_VERSION}."
        )

    def test_pyright_is_pinned_in_the_runtime_image(self) -> None:
        # Installed in the same `npm install -g` line, with the same unpinned-drift
        # exposure: it provides the `pyright-langserver` the pyright-lsp plugin execs.
        text = _RUNTIME_DOCKERFILE.read_text(encoding="utf-8")
        versions = {match.group("version") for match in _PYRIGHT_PATTERN.finditer(text)}
        assert versions == {_PYRIGHT_VERSION}, (
            f"deploy/Dockerfile must install pyright@{_PYRIGHT_VERSION}; got {versions or 'no install'}."
        )
