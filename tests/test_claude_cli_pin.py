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
import shutil
import subprocess
import tomllib
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest

from tests._git_repo import make_git_repo

_GIT = shutil.which("git") or "git"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SELF = Path(__file__).resolve()
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_RUNTIME_DOCKERFILE = _REPO_ROOT / "deploy" / "Dockerfile"

#: The SDK pin whose bundled CLI the eval/test tier tracks. When the SDK pin moves,
#: this constant reds and :data:`_SDK_BUNDLED_CLI_VERSION` must be re-derived from
#: the NEW wheel's ``claude_agent_sdk/_bundled/claude --version`` — never assumed.
_PINNED_SDK_VERSION = "0.2.139"

#: ``claude_agent_sdk/_bundled/claude --version`` from the wheel of
#: :data:`_PINNED_SDK_VERSION` → ``2.1.233 (Claude Code)``.
_SDK_BUNDLED_CLI_VERSION = "2.1.233"

#: The deployed runtime's pin: the version the factory host runs today.
_RUNTIME_CLI_VERSION = "2.1.233"

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
        # CI restores the uv package cache INTO the checkout; it never exists locally.
        ".uv-cache",
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


@cache
def _checkout_root(start: Path) -> Path:
    """The checkout owning *start* — the fork's root when core is vendored inside one.

    A fork vendors core as a plain subdirectory, so its own install sites sit ABOVE
    core's tree, where a walk rooted there cannot see them at all and this guard reads
    as covering CLI pinning while leaving them entirely unguarded.
    """
    try:
        toplevel = subprocess.run(
            [_GIT, "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return start
    if toplevel.returncode != 0 or not toplevel.stdout.strip():
        return start
    root = Path(toplevel.stdout.strip()).resolve()
    return root if root in {start, *start.parents} else start


def _core_site(name: str) -> str:
    """A core-relative site *name* as a scan-root-relative key."""
    prefix = _REPO_ROOT.relative_to(_checkout_root(_REPO_ROOT)).as_posix()
    return name if prefix == "." else f"{prefix}/{name}"


def _tracked_files(root: Path) -> list[Path] | None:
    """Paths git tracks under *root*, or ``None`` when *root* is not a checkout.

    The tracked tree IS the set of install sites this repo controls. A filesystem walk
    also reads whatever a build drops inside the checkout — CI points ``UV_CACHE_DIR``
    at ``$CI_PROJECT_DIR/.uv-cache``, so the unpacked ``claude_agent_sdk`` wheel's own
    error-message text ("npm install -g @anthropic-ai/claude-code") is read as an
    unpinned site of ours. Keying on tracking rather than on directory names means the
    next cache a runner drops in the project dir is out without another name to add.
    """
    try:
        listed = subprocess.run(
            [_GIT, "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if listed.returncode != 0:
        return None
    return [root / name for name in listed.stdout.split("\0") if name]


def _walked_files(root: Path) -> Iterator[Path]:
    stack = [root]
    while stack:
        for entry in sorted(stack.pop().iterdir()):
            # A symlinked directory here points back into the tree (a plugin checkout
            # linking its own root), so following one never ends.
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if not _is_skipped_dir(entry.name):
                    stack.append(entry)
            elif entry.is_file():
                yield entry


def _scannable_files(root: Path) -> Iterator[Path]:
    """The files under *root* this repo answers for — tracked, or walked off a non-checkout."""
    tracked = _tracked_files(root)
    candidates = tracked if tracked is not None else _walked_files(root)
    for path in candidates:
        if path.is_symlink() or not path.is_file() or path.resolve() == _SELF:
            continue
        yield path


def _scan_sites(root: Path) -> dict[str, str | None]:
    """Root-relative path → the pinned version, or ``None`` when unpinned."""
    sites: dict[str, str | None] = {}
    for path in _scannable_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in _INSTALL_PATTERN.finditer(text):
            sites[path.relative_to(root).as_posix()] = match.group("version")
    return sites


@cache
def _install_sites() -> dict[str, str | None]:
    """Scan-root-relative path → the pinned version, or ``None`` when unpinned."""
    return _scan_sites(_checkout_root(_REPO_ROOT))


def _sdk_pin() -> str:
    deps = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    matches = [d.replace(" ", "") for d in deps if d.replace(" ", "").startswith("claude-agent-sdk")]
    assert len(matches) == 1, f"expected exactly one claude-agent-sdk dependency, got {matches}"
    return matches[0].removeprefix("claude-agent-sdk==")


def _as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


class TestTheScanRootSpansTheWholeCheckout:
    """The walk must start at the checkout that OWNS core, standalone or vendored."""

    def test_a_vendored_core_is_scanned_from_the_forks_root(self, tmp_path: Path) -> None:
        core = tmp_path / "fork" / "vendor" / "teatree"
        core.mkdir(parents=True)
        make_git_repo(tmp_path / "fork")

        assert _checkout_root(core) == (tmp_path / "fork").resolve()

    def test_a_standalone_checkout_is_scanned_from_itself(self, tmp_path: Path) -> None:
        core = make_git_repo(tmp_path / "teatree")

        assert _checkout_root(core) == core.resolve()

    def test_no_checkout_at_all_still_scans_cores_own_tree(self, tmp_path: Path) -> None:
        # An sdist or an unpacked tarball has no git metadata; the guard must keep
        # covering core rather than resolve to nothing.
        loose = tmp_path / "loose"
        loose.mkdir()

        assert _checkout_root(loose) == loose


class TestOnlySitesThisRepoControlsAreScanned:
    @pytest.mark.parametrize("name", [".venv-3.13", ".uv-cache", "node_modules"])
    def test_a_dependency_tree_is_not_walked(self, name: str) -> None:
        assert _is_skipped_dir(name)

    def test_a_directory_this_repo_owns_is_walked(self) -> None:
        assert not _is_skipped_dir("src")


class TestTheScanCoversTrackedSourceOnly:
    """The walk answers for the tree git tracks — not for what a build unpacks inside it."""

    @staticmethod
    def _repo_with(tmp_path: Path, files: dict[str, str], *, tracked: list[str]) -> Path:
        repo = tmp_path / "fork"
        repo.mkdir()
        subprocess.run([_GIT, "init", "-q", "-b", "main", str(repo)], check=True)
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text, encoding="utf-8")
        subprocess.run([_GIT, "-C", str(repo), "add", "--", *tracked], check=True)
        return repo

    def test_an_unpinned_install_in_tracked_source_is_still_flagged(self, tmp_path: Path) -> None:
        repo = self._repo_with(
            tmp_path,
            {"deploy/Dockerfile": "RUN npm install -g @anthropic-ai/claude-code\n"},
            tracked=["deploy/Dockerfile"],
        )

        assert _scan_sites(repo) == {"deploy/Dockerfile": None}

    def test_a_pinned_install_in_tracked_source_reports_its_version(self, tmp_path: Path) -> None:
        repo = self._repo_with(
            tmp_path,
            {"deploy/Dockerfile": "RUN npm install -g @anthropic-ai/claude-code@2.1.220\n"},
            tracked=["deploy/Dockerfile"],
        )

        assert _scan_sites(repo) == {"deploy/Dockerfile": "2.1.220"}

    def test_an_unpacked_wheel_in_an_untracked_build_cache_is_not_a_site(self, tmp_path: Path) -> None:
        # CI points UV_CACHE_DIR inside the project dir; the SDK's own error text carries
        # a bare `npm install -g @anthropic-ai/claude-code` under a per-run hash segment.
        cache = ".uv-cache/archive-v0/3fcd8f2b9a1e/claude_agent_sdk/_internal/transport/subprocess_cli.py"
        repo = self._repo_with(
            tmp_path,
            {
                "deploy/Dockerfile": "RUN npm install -g @anthropic-ai/claude-code@2.1.220\n",
                cache: '    "  npm install -g @anthropic-ai/claude-code\\n"\n',
            },
            tracked=["deploy/Dockerfile"],
        )

        assert _scan_sites(repo) == {"deploy/Dockerfile": "2.1.220"}

    def test_a_tree_with_no_checkout_falls_back_to_walking_it(self, tmp_path: Path) -> None:
        # An sdist or unpacked tarball has no index to read; the guard must keep covering it.
        loose = tmp_path / "loose"
        (loose / "deploy").mkdir(parents=True)
        (loose / "deploy" / "Dockerfile").write_text("RUN npm install -g @anthropic-ai/claude-code\n")

        assert _scan_sites(loose) == {"deploy/Dockerfile": None}


class TestEveryInstallSiteIsPinned:
    def test_no_install_resolves_to_whatever_latest_is_today(self) -> None:
        unpinned = sorted(path for path, version in _install_sites().items() if version is None)
        assert not unpinned, (
            "every `npm install -g @anthropic-ai/claude-code` must carry an `@<version>` "
            "suffix — a bare install resolves to whatever `latest` is on build day, which "
            "no lockfile, guard, or bot can see. Unpinned: " + ", ".join(unpinned)
        )

    def test_every_core_site_is_classified_into_a_tier(self) -> None:
        # A new workflow that copies an existing install step lands here unclassified,
        # so it cannot silently inherit the wrong tier's version. Only CORE's own sites
        # are classified: a downstream checkout's tiers are its own to declare, and the
        # pinned-at-all assertion above already covers them.
        known = {_core_site(name) for name in _EVAL_TEST_SITES | _RUNTIME_SITES}
        core_prefix = _core_site("")
        discovered = {path for path in _install_sites() if path.startswith(core_prefix)}
        assert discovered == known, (
            "every Claude CLI install site must be classified as eval/test (pins the SDK-bundled "
            f"generation) or runtime (pins a current known-good version). New: {sorted(discovered - known)}; "
            f"gone: {sorted(known - discovered)}."
        )


class TestTheEvalTestTierTracksTheSdkBundle:
    def test_every_eval_test_site_pins_the_bundled_generation(self) -> None:
        sites = _install_sites()
        disagreeing = {
            name: sites.get(_core_site(name))
            for name in sorted(_EVAL_TEST_SITES)
            if sites.get(_core_site(name)) != _SDK_BUNDLED_CLI_VERSION
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
            name: sites.get(_core_site(name))
            for name in sorted(_RUNTIME_SITES)
            if sites.get(_core_site(name)) != _RUNTIME_CLI_VERSION
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
