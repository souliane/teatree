"""Tests for the publish-surface classifier (#126).

``teatree.hooks.publish_surface`` decides whether a HIGH/banned match on
a commit body should DOWNGRADE from hard-block to warn: the carve-out
applies to a ``git commit`` targeting a known-private repo, never to a
public posting surface, never on an unresolved body, and never on a body
carrying a secret. Detection is offline-first (a ``[teatree]
private_repos`` allowlist) with a cached ``gh``/``glab`` visibility probe
fallback.

Tests use a real ``git init`` repo under ``tmp_path`` with a rewritten
remote URL, plus a fake ``gh`` on PATH for the probe dimension.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from teatree.hooks import publish_surface
from teatree.hooks._command_parser import FAIL_CLOSED_SENTINEL


class _FakeHomePath:
    """Drop-in for the module's ``Path`` that pins ``home()`` to a tmp dir.

    Only ``publish_surface``'s ``Path(base)`` and ``Path.home()`` uses need
    to resolve; everything else delegates to the real ``pathlib.Path``, so a
    test can relocate the cache root without globally patching
    ``pathlib.Path.home`` (which would break pytest's own tmp machinery).
    """

    def __init__(self, home: Path) -> None:
        self._home = home

    def __call__(self, *args: object, **kwargs: object) -> Path:
        return Path(*args, **kwargs)

    def home(self) -> Path:
        return self._home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def _repo_with_remote(path: Path, remote_url: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "remote", "add", "origin", remote_url)
    return path


def _config(tmp_path: Path, private_repos: list[str]) -> Path:
    cfg = tmp_path / ".teatree.toml"
    entries = ", ".join(f'"{e}"' for e in private_repos)
    cfg.write_text(f"[teatree]\nprivate_repos = [{entries}]\n", encoding="utf-8")
    return cfg


def _make_gh_shim(bin_dir: Path, visibility: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "gh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"repo view"* && "$*" == *"visibility"* ]]; then\n'
        f'  echo "{visibility}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_glab_shim(bin_dir: Path, visibility: str) -> None:
    # glab 1.80.4 has NO ``--jq`` flag: passing it makes glab exit non-zero
    # with "Unknown flag". This shim mirrors that — it ONLY succeeds for the
    # bare ``glab api projects/...`` shape and emits the full project JSON
    # (the probe must parse ``.visibility`` in Python, not via ``--jq``).
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "glab"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"--jq"* ]]; then\n'
        '  echo "Unknown flag: --jq" >&2\n'
        "  exit 1\n"
        "fi\n"
        'if [[ "$*" == *"api projects/"* ]]; then\n'
        f'  echo \'{{"id": 42, "visibility": "{visibility}", "name": "r"}}\'\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _git_only_bin(bin_dir: Path) -> str:
    """Return a PATH with ``git`` available but no ``gh``/``glab`` probe tool.

    Clearing PATH outright would break the ``git remote get-url`` that
    resolves the slug; this keeps git reachable while guaranteeing the
    visibility probe finds no tool (so an unknown repo stays NOT private).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    real_git = shutil.which("git")
    assert real_git is not None
    (bin_dir / "git").symlink_to(real_git)
    return str(bin_dir)


class TestIsGitCommitCommand:
    def test_git_commit_is_recognised(self) -> None:
        assert publish_surface.is_git_commit_command('git commit -m "x"') is True

    def test_git_commit_with_env_prefix_is_recognised(self) -> None:
        assert publish_surface.is_git_commit_command('FOO=1 git commit -m "x"') is True

    def test_gh_issue_create_is_not_a_commit(self) -> None:
        assert publish_surface.is_git_commit_command('gh issue create --body "x"') is False

    def test_git_push_is_not_a_commit(self) -> None:
        assert publish_surface.is_git_commit_command("git push origin main") is False

    def test_commit_after_separator_does_not_count(self) -> None:
        # Only the FIRST segment is the command under classification.
        assert publish_surface.is_git_commit_command('echo hi && git commit -m "x"') is False


class TestPrivateRepoAllowlist:
    def test_namespace_substring_matches_repo_slug(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        repo = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is True

    def test_non_matching_repo_is_not_private_via_allowlist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        repo = _repo_with_remote(tmp_path / "r", "https://github.com/some/public.git")
        # No allowlist hit and no probe tool on PATH → unknown → NOT private.
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is False

    def test_no_remote_is_not_private(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        repo = tmp_path / "r"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is False

    def test_none_cwd_is_not_private(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        assert publish_surface.commit_targets_private_repo(None, config_path=cfg) is False


class TestVisibilityProbeFallback:
    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

    def test_private_visibility_probe_marks_repo_private(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, [])  # empty allowlist → must use the probe
        repo = _repo_with_remote(tmp_path / "r", "https://github.com/acme/secret.git")
        bin_dir = tmp_path / "bin"
        _make_gh_shim(bin_dir, "PRIVATE")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is True

    def test_public_visibility_probe_marks_repo_not_private(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, [])
        repo = _repo_with_remote(tmp_path / "r", "https://github.com/acme/open.git")
        bin_dir = tmp_path / "bin"
        _make_gh_shim(bin_dir, "PUBLIC")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is False

    def test_probe_verdict_is_cached(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, [])
        repo = _repo_with_remote(tmp_path / "r", "https://github.com/acme/cached.git")
        bin_dir = tmp_path / "bin"
        _make_gh_shim(bin_dir, "PRIVATE")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is True
        # Remove the shim — a fresh resolution must still answer from cache
        # (git stays available so the slug still resolves).
        (bin_dir / "gh").unlink()
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "gitonly"))
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is True

    def test_gitlab_private_probe_parses_json_without_jq(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bug 1: ``glab api`` has no ``--jq`` flag. The probe must request the
        # bare project JSON and parse ``.visibility`` in Python; passing
        # ``--jq`` (the old code) would make glab exit 1 → None → NOT private.
        cfg = _config(tmp_path, [])
        repo = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acme/secret.git")
        bin_dir = tmp_path / "bin"
        _make_glab_shim(bin_dir, "private")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is True

    def test_gitlab_public_probe_parses_json_without_jq(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, [])
        repo = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acme/open.git")
        bin_dir = tmp_path / "bin"
        _make_glab_shim(bin_dir, "public")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is False


class TestVisibilityCachePathCollision:
    """Bug 2: the cache must persist even when ``~/.teatree`` is a FILE.

    The historical default rooted the cache at ``~/.teatree`` — but that
    path is the shell-sourceable config FILE, not a directory, so every
    cache write raised "Not a directory" (swallowed as OSError) and the
    verdict could never persist. The default now lives under the XDG cache
    dir, which is collision-free.
    """

    def _patch_home_with_teatree_file(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home.mkdir(exist_ok=True)
        (home / ".teatree").write_text("# shell-sourceable config FILE\n", encoding="utf-8")
        monkeypatch.delenv("T3_DATA_DIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr(publish_surface, "Path", _FakeHomePath(home))

    def test_default_cache_root_avoids_home_teatree_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        self._patch_home_with_teatree_file(home, monkeypatch)
        root = publish_surface._cache_root()
        assert (home / ".teatree") not in root.parents
        assert root != home / ".teatree"

    def test_cache_round_trips_when_home_teatree_is_a_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        self._patch_home_with_teatree_file(home, monkeypatch)

        publish_surface._write_visibility_cache("gitlab.com/acme/secret", "PRIVATE")

        assert publish_surface._read_visibility_cache("gitlab.com/acme/secret") == "PRIVATE"
        assert (home / ".cache" / "teatree" / "repo-visibility-cache.json").is_file()

    def test_cache_falls_back_when_xdg_cache_teatree_is_a_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        (home / ".cache").mkdir(parents=True)
        (home / ".cache" / "teatree").write_text("not a dir\n", encoding="utf-8")
        monkeypatch.delenv("T3_DATA_DIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr(publish_surface, "Path", _FakeHomePath(home))

        publish_surface._write_visibility_cache("gitlab.com/acme/x", "PUBLIC")

        assert publish_surface._read_visibility_cache("gitlab.com/acme/x") == "PUBLIC"
        assert (home / ".teatree-data" / "repo-visibility-cache.json").is_file()


class TestContainsSecret:
    @pytest.mark.parametrize(
        "secret",
        [
            "ghp_" + "a" * 36,
            "github_pat_" + "b" * 70,
            "glpat-" + "c" * 24,
            "xoxb-" + "1" * 20,
            "AKIA" + "A" * 16,
            "AIza" + "x" * 35,
            "sk-" + "z" * 32,
            "-----BEGIN RSA PRIVATE KEY-----",
        ],
    )
    def test_known_secret_shapes_are_detected(self, secret: str) -> None:
        assert publish_surface.contains_secret(f"commit body with {secret} embedded") is True

    def test_ordinary_prose_is_not_a_secret(self) -> None:
        assert publish_surface.contains_secret("refactor the widget refinery for the bank") is False

    def test_empty_text_is_not_a_secret(self) -> None:
        assert publish_surface.contains_secret("") is False


class TestCarveOutApplies:
    @pytest.fixture
    def private_cfg(self, tmp_path: Path) -> Path:
        return _config(tmp_path, ["acmecorp-engineering"])

    @pytest.fixture
    def private_repo(self, tmp_path: Path) -> Path:
        return _repo_with_remote(tmp_path / "r", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")

    def test_private_repo_commit_with_domain_word_downgrades(self, private_cfg: Path, private_repo: Path) -> None:
        body = "fix the acmewidget refinery"
        assert (
            publish_surface.carve_out_applies(
                "Bash", f'git commit -m "{body}"', body, private_repo, config_path=private_cfg
            )
            is True
        )

    def test_public_posting_command_never_downgrades(self, private_cfg: Path, private_repo: Path) -> None:
        # A gh issue create from inside a private repo is STILL a public surface.
        assert (
            publish_surface.carve_out_applies(
                "Bash", 'gh issue create --body "acmewidget"', "acmewidget", private_repo, config_path=private_cfg
            )
            is False
        )

    def test_secret_in_body_blocks_carve_out(self, private_cfg: Path, private_repo: Path) -> None:
        body = "token is ghp_" + "a" * 36
        assert (
            publish_surface.carve_out_applies("Bash", 'git commit -m "x"', body, private_repo, config_path=private_cfg)
            is False
        )

    def test_unresolved_body_sentinel_blocks_carve_out(self, private_cfg: Path, private_repo: Path) -> None:
        assert (
            publish_surface.carve_out_applies(
                "Bash", "git commit -F /missing.txt", FAIL_CLOSED_SENTINEL, private_repo, config_path=private_cfg
            )
            is False
        )

    def test_public_repo_commit_does_not_downgrade(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        repo = _repo_with_remote(tmp_path / "r", "https://github.com/some/public.git")
        # No probe tool resolvable → unknown → NOT private.
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies("Bash", 'git commit -m "x"', "acmewidget", repo, config_path=cfg) is False
        )

    def test_non_bash_tool_does_not_downgrade(self, private_cfg: Path, private_repo: Path) -> None:
        verdict = publish_surface.carve_out_applies(
            "Write", "git commit", "acmewidget", private_repo, config_path=private_cfg
        )
        assert verdict is False
