"""Tests for the publish-surface classifier (#126).

``teatree.hooks.publish_surface`` decides whether a HIGH/banned match on
a commit body should DOWNGRADE from hard-block to warn: the carve-out
applies to a ``git commit`` targeting a known-private repo, never to a
public posting surface, never on an unresolved body, and never on a body
carrying a secret. Detection is offline-first (a ``[teatree]
private_repos`` allowlist) with a cached ``gh``/``glab`` visibility probe
fallback.

Extended in #1594: structured ``gh``/``glab`` PR/issue create-or-comment
commands are ALSO eligible when their RESOLVED TARGET repo is positively
known-private (via ``--repo``/``-R`` flag, or CWD fallback). Raw REST
(``gh api``, ``glab api``) and ``curl``/Slack remain ineligible. An
unknown or public target stays hard-blocked.

Tests use a real ``git init`` repo under ``tmp_path`` with a rewritten
remote URL, plus a fake ``gh`` on PATH for the probe dimension.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

from teatree.hooks import _repo_visibility, publish_surface
from teatree.hooks._command_parser import FAIL_CLOSED_SENTINEL


class _FakeHomePath:
    """Drop-in for the module's ``Path`` that pins ``home()`` to a tmp dir.

    Only ``_repo_visibility``'s ``Path(base)`` and ``Path.home()`` uses need
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


class TestIsGhGlabPostingCommand:
    def test_gh_pr_create_is_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("gh pr create --title x") is True

    def test_gh_issue_create_is_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("gh issue create --body x") is True

    def test_gh_issue_comment_is_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("gh issue comment 1 --body x") is True

    def test_gh_pr_comment_is_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("gh pr comment 1 --body x") is True

    def test_glab_mr_create_is_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("glab mr create --title x") is True

    def test_glab_issue_create_is_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("glab issue create --title x") is True

    def test_glab_mr_note_is_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("glab mr note 1 --message x") is True

    def test_gh_api_is_not_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("gh api repos/owner/repo/issues") is False

    def test_glab_api_is_not_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("glab api projects/owner%2Frepo") is False

    def test_gh_repo_view_is_not_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("gh repo view owner/repo") is False

    def test_glab_mr_list_is_not_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("glab mr list") is False

    def test_git_commit_is_not_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command('git commit -m "x"') is False

    def test_command_after_separator_is_now_seen(self) -> None:
        # INTENTIONAL CHANGE (#1657): the carve-out must SEE a posting verb
        # behind a leading prefix segment, else it over-blocks a legitimate
        # private-repo post (the scanner already scans the whole payload).
        # Previously asserted False (first-segment-only).
        assert publish_surface.is_gh_glab_posting_command("echo hi && gh pr create --title x") is True

    def test_prefixed_posting_segment_target_still_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Seeing the verb behind a prefix must NOT lose target resolution:
        # the explicit ``--repo`` of the posting segment still drives privacy,
        # and a public target behind a ``cd`` prefix stays NOT-private.
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        monkeypatch.delenv("GH_REPO", raising=False)
        assert (
            publish_surface.posting_command_targets_private_repo(
                "cd /x && gh pr create --repo acmecorp-engineering/p --title x", None, config_path=cfg
            )
            is True
        )
        assert (
            publish_surface.posting_command_targets_private_repo(
                "cd /x && gh pr create --repo souliane/teatree --title x", None, config_path=cfg
            )
            is False
        )


class TestExtractRepoFlag:
    """``_extract_repo_flag`` must mirror gh/glab's LAST-WINS resolution.

    gh/glab resolve a repeated ``--repo``/``-R`` flag by taking the LAST
    value, exactly like the ``-X GET -X POST`` method case. Reading the
    FIRST match would let a crafted command claim a private slug while gh
    actually posts to the trailing public one -- a leak.
    """

    def test_single_long_flag(self) -> None:
        assert publish_surface._extract_repo_flag(["--repo", "owner/name"]) == "owner/name"

    def test_single_equals_form(self) -> None:
        assert publish_surface._extract_repo_flag(["--repo=owner/name"]) == "owner/name"

    def test_single_short_flag(self) -> None:
        assert publish_surface._extract_repo_flag(["-R", "owner/name"]) == "owner/name"

    def test_short_equals_form(self) -> None:
        assert publish_surface._extract_repo_flag(["-R=owner/name"]) == "owner/name"

    def test_absent_flag_returns_empty(self) -> None:
        assert publish_surface._extract_repo_flag(["gh", "pr", "create", "--title", "x"]) == ""

    def test_repeated_long_flags_last_wins(self) -> None:
        words = ["--repo", "first/private", "--repo", "second/public"]
        assert publish_surface._extract_repo_flag(words) == "second/public"

    def test_repeated_equals_form_last_wins(self) -> None:
        words = ["--repo=first/private", "--repo=second/public"]
        assert publish_surface._extract_repo_flag(words) == "second/public"

    def test_mixed_long_then_short_last_wins(self) -> None:
        words = ["--repo=first/public", "-R", "second/private"]
        assert publish_surface._extract_repo_flag(words) == "second/private"

    def test_mixed_short_then_long_last_wins(self) -> None:
        words = ["-R", "first/private", "--repo=second/public"]
        assert publish_surface._extract_repo_flag(words) == "second/public"

    def test_short_equals_form_last_wins(self) -> None:
        words = ["-R=first/private", "-R=second/public"]
        assert publish_surface._extract_repo_flag(words) == "second/public"


class TestPrivateRepoAllowlist:
    def test_namespace_substring_matches_repo_slug(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        repo = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is True

    def test_non_matching_repo_is_not_private_via_allowlist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        repo = _repo_with_remote(tmp_path / "r", "git@github.com:some/public-repo.git")
        # No allowlist hit and no probe tool on PATH -> unknown -> NOT private.
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
        cfg = _config(tmp_path, [])  # empty allowlist -> must use the probe
        repo = _repo_with_remote(tmp_path / "r", "git@github.com:acme/secret-repo.git")
        bin_dir = tmp_path / "bin"
        _make_gh_shim(bin_dir, "PRIVATE")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is True

    def test_public_visibility_probe_marks_repo_not_private(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, [])
        repo = _repo_with_remote(tmp_path / "r", "git@github.com:acme/open-repo.git")
        bin_dir = tmp_path / "bin"
        _make_gh_shim(bin_dir, "PUBLIC")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is False

    def test_probe_verdict_is_cached(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, [])
        repo = _repo_with_remote(tmp_path / "r", "git@github.com:acme/cached-repo.git")
        bin_dir = tmp_path / "bin"
        _make_gh_shim(bin_dir, "PRIVATE")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is True
        # Remove the shim -- a fresh resolution must still answer from cache
        # (git stays available so the slug still resolves).
        (bin_dir / "gh").unlink()
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "gitonly"))
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is True

    def test_gitlab_private_probe_parses_json_without_jq(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bug 1: ``glab api`` has no ``--jq`` flag. The probe must request the
        # bare project JSON and parse ``.visibility`` in Python; passing
        # ``--jq`` (the old code) would make glab exit 1 -> None -> NOT private.
        cfg = _config(tmp_path, [])
        repo = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acme/secret-repo.git")
        bin_dir = tmp_path / "bin"
        _make_glab_shim(bin_dir, "private")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is True

    def test_gitlab_public_probe_parses_json_without_jq(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, [])
        repo = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acme/open-repo.git")
        bin_dir = tmp_path / "bin"
        _make_glab_shim(bin_dir, "public")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is False


class TestVisibilityCachePathCollision:
    """Bug 2: the cache must persist even when ``~/.teatree`` is a FILE.

    The historical default rooted the cache at ``~/.teatree`` -- but that
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
        monkeypatch.setattr(_repo_visibility, "Path", _FakeHomePath(home))

    def test_default_cache_root_avoids_home_teatree_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        self._patch_home_with_teatree_file(home, monkeypatch)
        root = _repo_visibility._cache_root()
        assert (home / ".teatree") not in root.parents
        assert root != home / ".teatree"

    def test_cache_round_trips_when_home_teatree_is_a_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        self._patch_home_with_teatree_file(home, monkeypatch)

        _repo_visibility._write_visibility_cache("gitlab.com/acme/secret", "PRIVATE")

        assert _repo_visibility._read_visibility_cache("gitlab.com/acme/secret") == "PRIVATE"
        assert (home / ".cache" / "teatree" / "repo-visibility-cache.json").is_file()

    def test_cache_falls_back_when_xdg_cache_teatree_is_a_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        (home / ".cache").mkdir(parents=True)
        (home / ".cache" / "teatree").write_text("not a dir\n", encoding="utf-8")
        monkeypatch.delenv("T3_DATA_DIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr(_repo_visibility, "Path", _FakeHomePath(home))

        _repo_visibility._write_visibility_cache("gitlab.com/acme/x", "PUBLIC")

        assert _repo_visibility._read_visibility_cache("gitlab.com/acme/x") == "PUBLIC"
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

    # SAFETY TEST: This test is load-bearing. A public-repo target MUST always
    # stay hard-blocked, regardless of CWD or allowlist.
    def test_gh_pr_create_explicit_public_repo_stays_hard_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        private_cwd = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")
        # No probe tool -> souliane/teatree is unknown -> treated as NOT private.
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "gh pr create --repo souliane/teatree --title x",
                "acmewidget fix",
                private_cwd,
                config_path=cfg,
            )
            is False
        )

    def test_gh_pr_create_explicit_private_repo_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # gh pr create targeting a known-private repo via --repo should downgrade.
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        # CWD repo is irrelevant when --repo is explicit; use an unrelated CWD.
        unrelated_cwd = _repo_with_remote(tmp_path / "r", "git@github.com:some/unrelated-repo.git")
        # No probe tool; acmecorp-engineering is in the allowlist -> private.
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "gh pr create --repo acmecorp-engineering/acmecorp-product --title x",
                "acmewidget fix",
                unrelated_cwd,
                config_path=cfg,
            )
            is True
        )

    def test_gh_pr_create_no_repo_flag_unknown_cwd_stays_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No --repo and CWD unknown (no probe tool) -> default-deny, stays hard-blocked.
        cfg = _config(tmp_path, [])  # empty allowlist
        unknown_cwd = _repo_with_remote(tmp_path / "r", "git@github.com:some/unknown-repo.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "gh pr create --title x",
                "acmewidget fix",
                unknown_cwd,
                config_path=cfg,
            )
            is False
        )

    def test_gh_api_is_not_eligible_for_carve_out(self, private_cfg: Path, private_repo: Path) -> None:
        # gh api (raw REST) is never eligible, even targeting a private repo.
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "gh api repos/acmecorp-engineering/acmecorp-product/issues --field body=x",
                "acmewidget",
                private_repo,
                config_path=private_cfg,
            )
            is False
        )

    def test_glab_mr_create_explicit_private_repo_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        unrelated_cwd = _repo_with_remote(tmp_path / "r", "git@github.com:some/unrelated-repo.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "glab mr create --repo acmecorp-engineering/acmecorp-product --title x",
                "acmewidget fix",
                unrelated_cwd,
                config_path=cfg,
            )
            is True
        )

    def test_gh_pr_create_no_repo_cwd_private_downgrades(self, private_cfg: Path, private_repo: Path) -> None:
        # gh pr create with no --repo falls back to CWD; private CWD -> downgrade.
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "gh pr create --title x",
                "acmewidget fix",
                private_repo,
                config_path=private_cfg,
            )
            is True
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
        repo = _repo_with_remote(tmp_path / "r", "git@github.com:some/public-repo.git")
        # No probe tool resolvable -> unknown -> NOT private.
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies("Bash", 'git commit -m "x"', "acmewidget", repo, config_path=cfg) is False
        )

    def test_non_bash_tool_does_not_downgrade(self, private_cfg: Path, private_repo: Path) -> None:
        verdict = publish_surface.carve_out_applies(
            "Write", "git commit", "acmewidget", private_repo, config_path=private_cfg
        )
        assert verdict is False

    # SAFETY TEST (the spoof): a crafted command that lists a private repo
    # FIRST and a PUBLIC repo LAST resolves (last-wins, like gh/glab) to the
    # PUBLIC repo, so the carve-out MUST NOT apply -- it stays hard-blocked.
    # Reading the first flag would leak the banned term to public teatree.
    def test_repeated_repo_private_then_public_stays_hard_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        private_cwd = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")
        # No probe tool -> the trailing public slug is unknown -> NOT private.
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "gh pr create --repo acmecorp-engineering/acmecorp-product --repo souliane/teatree --title x",
                "acmewidget fix",
                private_cwd,
                config_path=cfg,
            )
            is False
        )

    def test_repeated_repo_public_then_private_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reverse of the spoof: public FIRST, private LAST -> effective target
        # is the private repo (last-wins) -> downgrade. Proves last-wins both ways.
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        unrelated_cwd = _repo_with_remote(tmp_path / "r", "git@github.com:some/unrelated-repo.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "gh pr create --repo souliane/teatree --repo acmecorp-engineering/acmecorp-product --title x",
                "acmewidget fix",
                unrelated_cwd,
                config_path=cfg,
            )
            is True
        )

    def test_mixed_forms_public_equals_then_private_short_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``--repo=souliane/teatree -R acmecorp-engineering/...`` -> the short
        # ``-R`` private slug is LAST -> wins -> downgrade.
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        unrelated_cwd = _repo_with_remote(tmp_path / "r", "git@github.com:some/unrelated-repo.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "gh pr create --repo=souliane/teatree -R acmecorp-engineering/acmecorp-product --title x",
                "acmewidget fix",
                unrelated_cwd,
                config_path=cfg,
            )
            is True
        )

    def test_mixed_forms_private_short_then_public_equals_stays_hard_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``-R acmecorp-engineering/... --repo=souliane/teatree`` -> the public
        # ``--repo=`` slug is LAST -> wins -> stays hard-blocked.
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        private_cwd = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "gh pr create -R acmecorp-engineering/acmecorp-product --repo=souliane/teatree --title x",
                "acmewidget fix",
                private_cwd,
                config_path=cfg,
            )
            is False
        )

    # SAFETY TEST (the GH_REPO env leak): gh resolves its target from the
    # GH_REPO env var when no --repo flag is given. A public GH_REPO + a
    # flagless ``gh pr create`` from a PRIVATE CWD must NOT carve out -- gh
    # posts to the PUBLIC GH_REPO, so the banned term would leak. The hook
    # MUST honour GH_REPO BEFORE the CWD fallback, mirroring gh.
    def test_gh_env_repo_public_with_private_cwd_stays_hard_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        private_cwd = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")
        # No probe tool -> the public GH_REPO slug is unknown -> NOT private.
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        monkeypatch.setenv("GH_REPO", "souliane/teatree")
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "gh pr create --body x",
                "acmewidget fix",
                private_cwd,
                config_path=cfg,
            )
            is False
        )

    def test_gh_env_repo_private_with_unknown_cwd_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # GH_REPO points at a known-private repo, no --repo flag -> the env
        # target wins over the CWD fallback -> downgrade.
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        unknown_cwd = _repo_with_remote(tmp_path / "r", "git@github.com:some/unknown-repo.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        monkeypatch.setenv("GH_REPO", "acmecorp-engineering/acmecorp-product")
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "gh pr create --body x",
                "acmewidget fix",
                unknown_cwd,
                config_path=cfg,
            )
            is True
        )

    def test_gh_env_repo_unset_falls_back_to_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # GH_REPO unset -> behaviour is exactly the CWD fallback: a private
        # CWD with no --repo flag downgrades, as before this fix.
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        private_cwd = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        monkeypatch.delenv("GH_REPO", raising=False)
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "gh pr create --body x",
                "acmewidget fix",
                private_cwd,
                config_path=cfg,
            )
            is True
        )

    def test_explicit_repo_flag_wins_over_gh_env_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # An explicit ``--repo`` always wins over GH_REPO (gh ignores the env
        # var when the flag is present). ``--repo souliane/teatree`` (public)
        # + GH_REPO private -> the flag's public target wins -> stays blocked.
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        private_cwd = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        monkeypatch.setenv("GH_REPO", "acmecorp-engineering/acmecorp-product")
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "gh pr create --repo souliane/teatree --body x",
                "acmewidget fix",
                private_cwd,
                config_path=cfg,
            )
            is False
        )

    def test_glab_ignores_gh_env_repo_and_falls_back_to_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # glab does NOT honour GH_REPO. A flagless ``glab mr create`` with a
        # public GH_REPO exported must IGNORE GH_REPO and use the CWD origin.
        # Private CWD -> downgrade (GH_REPO public is irrelevant to glab).
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        private_cwd = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        monkeypatch.setenv("GH_REPO", "souliane/teatree")
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                "glab mr create --title x",
                "acmewidget fix",
                private_cwd,
                config_path=cfg,
            )
            is True
        )


# Slugs used across the golden corpus: the PRIVATE namespace is injected into
# the tmp allowlist; the PUBLIC slug is this repo, deliberately NOT allowlisted.
_PRIV_NS = "acmecorp-engineering"
_PRIV_SLUG = f"{_PRIV_NS}/acmecorp-product"
_PUBLIC_SLUG = "souliane/teatree"
_PRIV_REMOTE = f"git@gitlab.com:{_PRIV_SLUG}.git"
_UNKNOWN_REMOTE = "git@github.com:some/unknown-repo.git"
_TERM = "acmewidget"
_FAKE_SECRET = "ghp_" + "a" * 36


class _CorpusRow(NamedTuple):
    """One golden-corpus case: command + body and the CWD remote.

    The expected verdict is implicit in which tuple the row lives in
    (``_MUST_ALLOW`` => downgrade, ``_MUST_DENY`` => hard-block), so there is
    no boolean field to pass positionally.
    """

    case: str
    command: str
    payload: str
    cwd_remote: str


# must-ALLOW: a private-target post/commit downgrades to warn. These prove the
# over-block is fixed -- prefixed / env / cd-prefixed posting verbs are seen.
_MUST_ALLOW: tuple[_CorpusRow, ...] = (
    _CorpusRow("A1", f'gh issue create --repo {_PRIV_SLUG} --body "{_TERM}"', _TERM, _PRIV_REMOTE),
    _CorpusRow("A2", f'cd /x && gh issue create --repo {_PRIV_SLUG} --body "{_TERM}"', _TERM, _PRIV_REMOTE),
    _CorpusRow("A3", f'ENV=1 gh issue create --repo {_PRIV_SLUG} --body "{_TERM}"', _TERM, _PRIV_REMOTE),
    _CorpusRow("A4", f'gh issue create --body "{_TERM}"', _TERM, _PRIV_REMOTE),
    _CorpusRow("A5", f"cd sub && gh pr create --repo {_PRIV_SLUG} --body x", _TERM, _PRIV_REMOTE),
    _CorpusRow("A6", f'git commit -m "{_TERM}"', _TERM, _PRIV_REMOTE),
    _CorpusRow("A7", f'glab mr create --repo {_PRIV_SLUG} --description "{_TERM}"', _TERM, _PRIV_REMOTE),
    _CorpusRow(
        "A8",
        f'gh issue create --repo {_PRIV_SLUG} --body "see (gh issue 5) and glab notes here"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "A9",
        f'gh issue create --repo {_PRIV_SLUG} --title "refs (gh issue 5) glab note" --body "{_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "A10",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && gh issue create --repo {_PRIV_SLUG} --body ok2",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "A11",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && gh issue view 5 --repo {_PRIV_SLUG}",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "A12",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && gh pr comment 5 --repo {_PRIV_SLUG} --body ok2",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "A13",
        f'gh issue create --repo {_PRIV_SLUG} --body ok && sh -c "date"',
        _TERM,
        _PRIV_REMOTE,
    ),
)

# must-DENY: the load-bearing under-block guards. A public/unknown target, a
# raw-REST segment, a secret, a chained public posting segment, or the
# commit-plus-public-post guard must ALL stay hard-blocked.
_MUST_DENY: tuple[_CorpusRow, ...] = (
    _CorpusRow("D1", f'cd /x && gh issue create --repo {_PUBLIC_SLUG} --body "{_TERM}"', _TERM, _PRIV_REMOTE),
    _CorpusRow("D2", f"ENV=1 gh issue create --repo {_PUBLIC_SLUG} --body x", _TERM, _PRIV_REMOTE),
    _CorpusRow("D3", f"gh issue create --repo {_PUBLIC_SLUG}", _TERM, _PRIV_REMOTE),
    _CorpusRow("D4", f'gh issue create --body "{_TERM}"', _TERM, _UNKNOWN_REMOTE),
    _CorpusRow("D5", f"gh api repos/{_PRIV_SLUG}/issues -f body={_TERM}", _TERM, _PRIV_REMOTE),
    _CorpusRow("D6", "glab api projects/x -X POST", _TERM, _PRIV_REMOTE),
    _CorpusRow("D7", 'git commit -m "x"', f"body has {_FAKE_SECRET} embedded", _PRIV_REMOTE),
    _CorpusRow("D8", f'gh issue create --repo {_PRIV_SLUG} --body "{_FAKE_SECRET}"', _FAKE_SECRET, _PRIV_REMOTE),
    _CorpusRow(
        "D9",
        f"gh issue create --repo {_PRIV_SLUG} --body x && gh issue create --repo {_PUBLIC_SLUG} --body x",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "D10",
        f"gh issue create --repo {_PRIV_SLUG} && gh api repos/x/issues -f body=x",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow("D11", f"gh issue create --repo {_PRIV_SLUG} --repo {_PUBLIC_SLUG}", _TERM, _PRIV_REMOTE),
    _CorpusRow(
        "D12",
        f'git commit -m "{_TERM}" && gh issue create --repo {_PUBLIC_SLUG} --body "{_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "D13",
        f'gh issue create --repo {_PRIV_SLUG} --body ok && (gh issue create --repo {_PUBLIC_SLUG} --body "{_TERM}")',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "D14",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& echo $(gh issue create --repo {_PUBLIC_SLUG} --body "{_TERM}")',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "D15",
        f"cd /tmp && gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& (gh issue create --repo {_PUBLIC_SLUG} --body "{_TERM}")',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "D16",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && echo `gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}`",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "D17",
        f'gh issue create --repo {_PRIV_SLUG} --body "$(gh issue create --repo {_PUBLIC_SLUG} --body {_TERM})"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "D18",
        f"glab issue create --repo {_PRIV_SLUG} --description ok "
        f"&& (glab issue create --repo {_PUBLIC_SLUG} --description {_TERM})",
        _TERM,
        _PRIV_REMOTE,
    ),
    # L1-L13: the wrapper / process-substitution / wrapper-word under-block
    # leaks the prefix-strip guard missed. A bare ``gh`` token whose segment's
    # words[0] is the opener/wrapper-word evades the recognised-segment scan, so
    # a PUBLIC-targeting post hides behind the private one. The count invariant
    # (more gh/glab command-words than recognised gh/glab segments) fails closed.
    _CorpusRow(
        "L1",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && ( gh issue create --repo {_PUBLIC_SLUG} --body {_TERM} )",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "L2",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && (  gh issue create --repo {_PUBLIC_SLUG} --body {_TERM} )",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "L3",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && (\tgh issue create --repo {_PUBLIC_SLUG} --body {_TERM} )",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "L4",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && $( gh issue create --repo {_PUBLIC_SLUG} --body {_TERM} )",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "L5",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f"&& echo $( gh issue create --repo {_PUBLIC_SLUG} --body {_TERM} )",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "L6",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && {{ gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}; }}",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "L7",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && cat <(gh issue create --repo {_PUBLIC_SLUG} --body {_TERM})",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "L8",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && tee >(gh issue create --repo {_PUBLIC_SLUG} --body {_TERM})",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "L9",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && cat =(gh issue create --repo {_PUBLIC_SLUG} --body {_TERM})",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "L10",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && eval gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "L11",
        f"gh issue create --repo {_PRIV_SLUG} --body ok | xargs gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "L12",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f"&& env FOO=x gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "L13",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f"&& command gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}",
        _TERM,
        _PRIV_REMOTE,
    ),
    # S1-S4: the shell-string ``-c`` under-block the count invariant misses.
    # The inner ``gh`` lives wholly inside the quoted ``-c`` argument, so it is
    # ONE WORD token that does NOT strip to ``gh``/``glab`` (T not raised) and the
    # ``sh``/``bash``/``zsh`` segment's words[0] is the shell, not gh/glab (R not
    # raised), and there is no ``$(gh`` marker -- so the count check alone passes
    # it. Re-tokenizing the ``-c`` argument and checking for an inner gh/glab
    # command-word fails closed on all four shell + flag variants.
    _CorpusRow(
        "S1",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& sh -c "gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "S2",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& bash -c "gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "S3",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& zsh -c "gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "S4",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& sh -lc "gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
)


class TestCarveOutGoldenCorpus:
    """HERMETIC golden must-ALLOW / must-DENY corpus for the carve-out.

    The binding durable artifact for the segment-scan over-block fix and the
    under-block guards. Fully offline: ``gh``/``glab`` are ABSENT from PATH and
    ``_PROBE_PATH_EXTRA`` is emptied, so any non-allowlisted slug resolves
    NOT-private deterministically (no network). The PRIVATE namespace is
    injected into the tmp allowlist; the PUBLIC slug is never allowlisted.
    """

    @pytest.fixture(autouse=True)
    def _offline_probe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # No gh/glab on PATH and no augmented-path fallback -> the probe finds
        # no tool -> any non-allowlisted slug is NOT private (deterministic).
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "probebin"))
        monkeypatch.setattr(_repo_visibility, "_PROBE_PATH_EXTRA", ())
        monkeypatch.delenv("GH_REPO", raising=False)
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

    def _verdict(self, row: _CorpusRow, tmp_path: Path) -> bool:
        cfg = _config(tmp_path, [_PRIV_NS])
        cwd = _repo_with_remote(tmp_path / "cwd", row.cwd_remote)
        return publish_surface.carve_out_applies("Bash", row.command, row.payload, cwd, config_path=cfg)

    @pytest.mark.parametrize("row", _MUST_ALLOW, ids=lambda r: r.case)
    def test_must_allow_downgrades(self, row: _CorpusRow, tmp_path: Path) -> None:
        assert self._verdict(row, tmp_path) is True, f"{row.case}: expected downgrade (carve-out applies)"

    @pytest.mark.parametrize("row", _MUST_DENY, ids=lambda r: r.case)
    def test_must_deny_stays_hard_blocked(self, row: _CorpusRow, tmp_path: Path) -> None:
        assert self._verdict(row, tmp_path) is False, f"{row.case}: expected hard-block (carve-out must NOT apply)"


class TestShellCStringHidesGhGlab:
    """The scoped ``sh -c "gh ..."`` command-string fail-closed detector."""

    @pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash", "ksh", "ash"])
    @pytest.mark.parametrize("flag", ["-c", "-lc", "-ic", "-xc"])
    def test_inner_gh_in_c_string_fails_closed(self, shell: str, flag: str) -> None:
        cmd = f'gh issue create --repo {_PRIV_SLUG} --body ok && {shell} {flag} "gh issue create --repo {_PUBLIC_SLUG}"'
        assert publish_surface._command_hides_gh_glab(cmd) is True

    def test_inner_glab_in_c_string_fails_closed(self) -> None:
        cmd = f'gh issue create --repo {_PRIV_SLUG} --body ok && bash -c "glab mr create --repo {_PUBLIC_SLUG}"'
        assert publish_surface._command_hides_gh_glab(cmd) is True

    def test_shell_c_string_without_inner_gh_does_not_block(self) -> None:
        assert publish_surface._command_hides_gh_glab('sh -c "date"') is False
        cmd = f'gh issue create --repo {_PRIV_SLUG} --body ok && sh -c "echo done"'
        assert publish_surface._command_hides_gh_glab(cmd) is False

    def test_shell_segment_without_c_flag_does_not_block(self) -> None:
        # ``sh script.sh gh`` -- no ``-c`` flag, so the command-string detector
        # finds nothing to re-tokenize; the count invariant covers a real gh
        # token elsewhere.
        assert publish_surface._command_hides_gh_glab("sh script.sh") is False
        # ``-e`` precedes the ``-c`` flag: a non-c flag is skipped before the
        # ``-c`` argument is re-tokenized.
        cmd = f'gh issue create --repo {_PRIV_SLUG} --body ok && bash -e -c "gh issue create --repo {_PUBLIC_SLUG}"'
        assert publish_surface._command_hides_gh_glab(cmd) is True

    def test_prose_body_with_literal_gh_word_does_not_over_block(self) -> None:
        cmd = f'gh issue create --repo {_PRIV_SLUG} --body "run gh issue list later"'
        assert publish_surface._command_hides_gh_glab(cmd) is False

    def test_runtime_resolved_verbs_are_accepted_static_limits(self) -> None:
        # Documented limitations: a static gate that cannot execute the shell
        # cannot see a verb produced at runtime. These are NOT caught by design.
        var_indirection = (
            f'gh issue create --repo {_PRIV_SLUG} --body ok && G=gh; "$G" issue create --repo {_PUBLIC_SLUG}'
        )
        subst_verb = f"gh issue create --repo {_PRIV_SLUG} --body ok && $(echo gh) issue create --repo {_PUBLIC_SLUG}"
        assert publish_surface._command_hides_gh_glab(var_indirection) is False
        assert publish_surface._command_hides_gh_glab(subst_verb) is False


class TestProbeEnvResolution:
    """G2 — the probe resolves its tool against the augmented PATH.

    The PreToolUse subprocess inherits a restricted PATH; a bare ``gh`` may not
    resolve even though it is installed under a homebrew/local bin. The probe
    augments PATH with ``_PROBE_PATH_EXTRA`` before ``shutil.which``, so a tool
    absent from PATH but present in an extra dir still resolves PRIVATE.
    """

    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

    def test_probe_resolves_tool_from_extra_path_not_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, [])  # empty allowlist -> must use the probe
        repo = _repo_with_remote(tmp_path / "r", "git@github.com:acme/secret-repo.git")
        # gh shim lives in an EXTRA dir, NOT on PATH (only git is on PATH).
        extra_bin = tmp_path / "extra"
        _make_gh_shim(extra_bin, "PRIVATE")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "gitonly"))
        # The extra dir holds the gh shim; the standard dirs let the shim's
        # ``#!/usr/bin/env bash`` shebang resolve from the augmented probe env.
        monkeypatch.setattr(_repo_visibility, "_PROBE_PATH_EXTRA", (str(extra_bin), "/usr/bin", "/bin"))
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is True

    def test_visibility_unknown_returns_slug_when_probe_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, [])  # not allowlisted
        repo = _repo_with_remote(tmp_path / "r", _PRIV_REMOTE)
        # No probe tool anywhere -> visibility is unknown in-hook.
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "gitonly"))
        monkeypatch.setattr(_repo_visibility, "_PROBE_PATH_EXTRA", ())
        slug = publish_surface.visibility_unknown_for_block(
            f"gh issue create --repo {_PRIV_SLUG} --body x", repo, config_path=cfg
        )
        assert slug == _PRIV_SLUG

    def test_visibility_unknown_returns_none_when_allowlisted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, [_PRIV_NS])  # allowlisted -> known private -> not "unknown"
        repo = _repo_with_remote(tmp_path / "r", _PRIV_REMOTE)
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "gitonly"))
        monkeypatch.setattr(_repo_visibility, "_PROBE_PATH_EXTRA", ())
        slug = publish_surface.visibility_unknown_for_block(
            f"gh issue create --repo {_PRIV_SLUG} --body x", repo, config_path=cfg
        )
        assert slug is None

    def test_visibility_unknown_returns_none_when_genuinely_public(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A genuinely PUBLIC target (probe resolves PUBLIC) is correctly
        # blocked, not "unknown" -- emitting the add-to-allowlist hint there
        # would be misleading, so no slug is returned.
        cfg = _config(tmp_path, [])
        repo = _repo_with_remote(tmp_path / "r", "git@github.com:acme/open-repo.git")
        extra_bin = tmp_path / "extra"
        _make_gh_shim(extra_bin, "PUBLIC")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "gitonly"))
        monkeypatch.setattr(_repo_visibility, "_PROBE_PATH_EXTRA", (str(extra_bin), "/usr/bin", "/bin"))
        slug = publish_surface.visibility_unknown_for_block(
            "gh issue create --repo acme/open-repo --body x", repo, config_path=cfg
        )
        assert slug is None
