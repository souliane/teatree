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

Reworked in #1657 to an ALLOWLIST: the posting path now decides via
``command_is_pure_private_gh_glab_post`` -- a single positive proof that the
WHOLE command is a pure private ``gh``/``glab`` post -- instead of an
enumerated set of execution introducers the gate tried to DETECT. The golden
corpus is the two-dimensional contract: a must-ALLOW set (every legit private
post still downgrades -- the over-block guard) and a must-DENY set (any
transport, public target, raw REST, secret, or NOVEL mechanism hard-blocks --
the leak guard, transport-agnostically).

Tests use a real ``git init`` repo under ``tmp_path`` with a rewritten
remote URL, plus a fake ``gh`` on PATH for the probe dimension.
"""

import json
import os
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

from teatree.hooks import _gh_glab_hiding, _repo_visibility, publish_surface
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
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'private_repos', ?)",
            (json.dumps(private_repos),),
        )
        conn.commit()
    finally:
        conn.close()
    return db


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

    def test_git_dash_c_commit_is_recognised(self) -> None:
        assert publish_surface.is_git_commit_command('git -C /some/worktree commit -m "x"') is True

    def test_git_git_dir_commit_is_recognised(self) -> None:
        assert publish_surface.is_git_commit_command('git --git-dir=/x/.git commit -m "x"') is True

    def test_git_work_tree_commit_is_recognised(self) -> None:
        assert publish_surface.is_git_commit_command('git --work-tree=/x commit -m "x"') is True

    def test_git_dash_c_separate_value_commit_is_recognised(self) -> None:
        assert publish_surface.is_git_commit_command('git --git-dir /x/.git commit -m "x"') is True

    def test_env_prefix_and_global_flags_combined_is_recognised(self) -> None:
        assert publish_surface.is_git_commit_command('FOO=1 git -C /some/worktree commit -m "x"') is True

    def test_git_dash_c_status_is_not_a_commit(self) -> None:
        assert publish_surface.is_git_commit_command("git -C /some/worktree status") is False


class TestEffectiveRepoDir:
    """``effective_repo_dir`` resolves the dir whose repo the commit LANDS in.

    ``git`` selects a commit's repo from ``--git-dir``/``$GIT_DIR`` if given,
    else from the repo discovered at the ``-C``-adjusted working directory.
    ``--work-tree`` only sets the working tree and NEVER selects the repo, so
    it must not contribute repo identity here. A sub-agent's
    ``git -C <worktree> commit`` runs from an ambient hook ``cwd`` that has
    reset to ``~/workspace``, so the repo the commit lands in lives in the
    command's own ``-C``/``--git-dir`` flags.
    """

    def test_dash_c_separate_value(self) -> None:
        assert publish_surface.effective_repo_dir("git -C /some/worktree commit -m x") == "/some/worktree"

    def test_git_dir_separate_value(self) -> None:
        assert publish_surface.effective_repo_dir("git --git-dir /x/.git commit -m x") == "/x/.git"

    def test_git_dir_equals_form(self) -> None:
        assert publish_surface.effective_repo_dir("git --git-dir=/x/.git commit -m x") == "/x/.git"

    def test_absent_returns_none(self) -> None:
        assert publish_surface.effective_repo_dir('git commit -m "x"') is None

    def test_repeated_absolute_dash_c_resets(self) -> None:
        # An absolute subsequent ``-C`` resets the accumulator (git semantics),
        # so the last absolute value wins.
        assert publish_surface.effective_repo_dir("git -C /first -C /second commit -m x") == "/second"

    def test_repeated_relative_dash_c_is_cumulative(self) -> None:
        # git: each subsequent NON-absolute ``-C <path>`` is interpreted
        # relative to the preceding ``-C <path>``. The commit lands in
        # ``/pub/relpriv``, NOT the bare last segment ``relpriv``.
        assert publish_surface.effective_repo_dir("git -C /pub -C relpriv commit -m x") == "/pub/relpriv"

    def test_absolute_dash_c_after_relative_resets(self) -> None:
        # An absolute value anywhere in the chain resets the accumulator.
        assert publish_surface.effective_repo_dir("git -C rel -C /abs commit -m x") == "/abs"

    def test_three_relative_dash_c_accumulate(self) -> None:
        assert publish_surface.effective_repo_dir("git -C /a -C b -C c commit -m x") == "/a/b/c"

    def test_dash_c_equals_form_accumulates(self) -> None:
        assert publish_surface.effective_repo_dir("git -C=/pub -C=relpriv commit -m x") == "/pub/relpriv"

    def test_unresolvable_dash_c_value_is_fail_closed(self) -> None:
        # A ``-C`` value carrying a substitution marker cannot be resolved
        # statically -> fail-closed sentinel so the carve-out never downgrades.
        assert (
            publish_surface.effective_repo_dir("git -C /pub -C $(echo x) commit -m x")
            == publish_surface.UNRESOLVABLE_REPO_DIR
        )

    def test_repeated_git_dir_last_wins(self) -> None:
        command = "git --git-dir=/first/.git --git-dir=/second/.git commit"
        assert publish_surface.effective_repo_dir(command) == "/second/.git"

    def test_work_tree_alone_never_selects_repo(self) -> None:
        # ``--work-tree`` only sets the working tree; the repo is discovered
        # from the (unchanged) cwd, so this resolver returns None and the
        # caller falls back to the ambient cwd.
        assert publish_surface.effective_repo_dir("git --work-tree=/some/worktree commit -m x") is None

    def test_work_tree_does_not_override_git_dir(self) -> None:
        # Repo identity comes from ``--git-dir`` regardless of where the
        # ``--work-tree`` flag sits relative to it (order-independent).
        assert publish_surface.effective_repo_dir("git --git-dir=/pub/.git --work-tree=/priv commit") == "/pub/.git"
        assert publish_surface.effective_repo_dir("git --work-tree=/priv --git-dir=/pub/.git commit") == "/pub/.git"

    def test_work_tree_does_not_override_dash_c(self) -> None:
        assert publish_surface.effective_repo_dir("git -C /repo --work-tree=/priv commit") == "/repo"
        assert publish_surface.effective_repo_dir("git --work-tree=/priv -C /repo commit") == "/repo"

    def test_git_dir_wins_over_dash_c(self) -> None:
        # ``--git-dir`` names the repo directly, overriding the repo that
        # would otherwise be discovered from the ``-C``-adjusted cwd.
        assert publish_surface.effective_repo_dir("git -C /work --git-dir=/repo/.git commit") == "/repo/.git"

    def test_relative_git_dir_resolved_against_dash_c(self) -> None:
        # A relative ``--git-dir`` is resolved against the ``-C``-adjusted cwd.
        assert publish_surface.effective_repo_dir("git -C /work --git-dir=sub/.git commit") == "/work/sub/.git"


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

    def test_gh_pr_edit_is_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("gh pr edit 1 --body x") is True

    def test_gh_issue_edit_is_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("gh issue edit 1 --body x") is True

    def test_glab_mr_update_is_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("glab mr update 1 --description x") is True

    def test_glab_issue_update_is_eligible(self) -> None:
        assert publish_surface.is_gh_glab_posting_command("glab issue update 1 --description x") is True

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
            publish_surface.command_is_pure_private_gh_glab_post(
                "cd /x && gh pr create --repo acmecorp-engineering/p --title x", None, config_path=cfg
            )
            is True
        )
        assert (
            publish_surface.command_is_pure_private_gh_glab_post(
                "cd /x && gh pr create --repo souliane/teatree --title x", None, config_path=cfg
            )
            is False
        )

    def test_host_qualified_allowlist_downgrades_bare_repo_flag_post(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #2067: a host-qualified ``private_repos`` entry (the doc'd / cwd form)
        # must downgrade a ``gh pr create --repo owner/name`` (bare flag form).
        cfg = _config(tmp_path, ["github.com/acmecorp-engineering"])
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        monkeypatch.delenv("GH_REPO", raising=False)
        assert (
            publish_surface.command_is_pure_private_gh_glab_post(
                "gh pr create --repo acmecorp-engineering/product --title x --body y", None, config_path=cfg
            )
            is True
        )

    def test_host_qualified_allowlist_does_not_downgrade_public_repo_flag_post(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["github.com/acmecorp-engineering"])
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        monkeypatch.delenv("GH_REPO", raising=False)
        assert (
            publish_surface.command_is_pure_private_gh_glab_post(
                "gh pr create --repo souliane/teatree --title x --body y", None, config_path=cfg
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


class TestAllowlistHostQualificationSymmetry:
    """A host-qualified ``private_repos`` entry must match a bare ``--repo`` slug.

    The carve-out doc states an entry is matched against a repo's origin slug
    ``host/owner/repo``, and ``slug_for_cwd`` emits exactly that host-qualified
    form -- so a user (or the cwd/commit path) supplies ``host/owner/repo``. But
    ``gh pr create --repo`` takes a BARE ``owner/repo`` slug. The plain
    ``entry in slug`` substring check is asymmetric: a host-qualified entry is a
    substring of a host-qualified slug but NOT of a bare one, so the SAME private
    repo downgrades on commit (cwd slug) yet hard-blocks on pr-create (#2067).
    The match must normalize the host prefix on BOTH sides.
    """

    def test_host_qualified_entry_matches_bare_repo_flag_slug(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["github.com/acmecorp-engineering"])
        assert _repo_visibility.slug_is_allowlisted_private("acmecorp-engineering/product", cfg) is True

    def test_host_qualified_entry_still_matches_host_qualified_slug(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["github.com/acmecorp-engineering"])
        assert _repo_visibility.slug_is_allowlisted_private("github.com/acmecorp-engineering/product", cfg) is True

    def test_bare_org_entry_matches_both_slug_forms(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        assert _repo_visibility.slug_is_allowlisted_private("acmecorp-engineering/product", cfg) is True
        assert _repo_visibility.slug_is_allowlisted_private("github.com/acmecorp-engineering/product", cfg) is True

    def test_unrelated_public_slug_still_not_allowlisted(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["github.com/acmecorp-engineering"])
        assert _repo_visibility.slug_is_allowlisted_private("souliane/teatree", cfg) is False
        assert _repo_visibility.slug_is_allowlisted_private("github.com/souliane/teatree", cfg) is False

    def test_bare_host_root_entry_does_not_downgrade_any_repo(self, tmp_path: Path) -> None:
        # B1: a malformed entry ``host./`` host-strips to "" — without filtering
        # the empty form, ``"" in slug`` is always True and EVERY repo (incl.
        # public) would downgrade. The empty form must be dropped on both sides.
        cfg = _config(tmp_path, ["github.com/"])
        assert _repo_visibility.slug_is_allowlisted_private("souliane/teatree", cfg) is False
        assert _repo_visibility.slug_is_allowlisted_private("github.com/souliane/teatree", cfg) is False
        assert _repo_visibility.slug_is_allowlisted_private("acmecorp-engineering/product", cfg) is False


class TestAllowlistSshAliasAndSupersetSlug:
    """The allowlist match is path-segment-aware, not a raw substring (#1953).

    A bare ``entry in slug`` substring check misclassifies a PUBLIC repo as
    private whenever the entry happens to appear as a substring of an unrelated
    part of the slug -- an SSH host alias (``gitlab-<entry>:org/public``) or a
    superset owner (``<entry>-fork/repo``, ``open<entry>/repo``). That downgrades
    the banned-terms gate on a public surface -- the leak-direction bug. The fix
    matches the entry against the slug's owner/repo PATH SEGMENTS only (the host
    segment never participates), as an exact leading-segment-prefix.
    """

    def test_ssh_alias_host_containing_entry_does_not_match(self, tmp_path: Path) -> None:
        # The canonical bug: a PUBLIC repo whose ``origin`` uses an SSH config
        # Host ALIAS (``gitlab-acmecorp``) -- a local name with no canonical
        # identity -- that merely CONTAINS the allowlist entry as a substring.
        cfg = _config(tmp_path, ["acmecorp"])
        assert _repo_visibility.slug_is_allowlisted_private("gitlab-acmecorp/someorg/public-repo", cfg) is False

    def test_superset_owner_segment_does_not_match(self, tmp_path: Path) -> None:
        # ``acme-engineering`` must NOT match a different org ``acme-engineering-fork``.
        cfg = _config(tmp_path, ["acme-engineering"])
        assert _repo_visibility.slug_is_allowlisted_private("github.com/acme-engineering-fork/widget", cfg) is False
        assert _repo_visibility.slug_is_allowlisted_private("acme-engineering-fork/widget", cfg) is False

    def test_substring_owner_segment_does_not_match(self, tmp_path: Path) -> None:
        # ``acmecorp`` must NOT match an owner that merely CONTAINS it (``openacmecorp``).
        cfg = _config(tmp_path, ["acmecorp"])
        assert _repo_visibility.slug_is_allowlisted_private("github.com/openacmecorp/widget", cfg) is False
        assert _repo_visibility.slug_is_allowlisted_private("openacmecorp/widget", cfg) is False

    def test_genuine_private_org_namespace_still_matches(self, tmp_path: Path) -> None:
        # The must-MATCH side: a real private repo under the configured namespace
        # still downgrades, in every slug form, host-qualified and bare.
        cfg = _config(tmp_path, ["acme-engineering"])
        assert _repo_visibility.slug_is_allowlisted_private("github.com/acme-engineering/secret", cfg) is True
        assert _repo_visibility.slug_is_allowlisted_private("acme-engineering/secret", cfg) is True
        assert _repo_visibility.slug_is_allowlisted_private("gitlab.com/acme-engineering/secret", cfg) is True

    def test_whole_owner_repo_entry_still_matches(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["acme-engineering/secret"])
        assert _repo_visibility.slug_is_allowlisted_private("acme-engineering/secret", cfg) is True
        assert _repo_visibility.slug_is_allowlisted_private("github.com/acme-engineering/secret", cfg) is True
        # A different repo under the same owner is NOT covered by a whole-repo entry.
        assert _repo_visibility.slug_is_allowlisted_private("acme-engineering/other", cfg) is False


class TestSlugForCwdSshAliasNormalization:
    """``slug_for_cwd`` normalizes an SSH config Host ALIAS remote (#1953).

    A standard SSH remote ``git@host:owner/repo`` already normalizes to
    ``host/owner/repo`` because it carries a ``user@`` part. An SSH *alias*
    remote ``alias:owner/repo`` (the ``Host alias`` form from ``~/.ssh/config``,
    no ``user@``) was previously returned verbatim (``alias:owner/repo``) with
    the ``:`` glued in -- a non-canonical slug whose alias segment then tripped
    the substring matcher. The alias is a local config name with no canonical
    identity, so it is dropped; the canonical slug is the bare ``owner/repo``.
    """

    def test_ssh_alias_remote_drops_alias_and_keeps_owner_repo(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "r", "gitlab-acmecorp:someorg/public-repo.git")
        assert _repo_visibility.slug_for_cwd(repo) == "someorg/public-repo"

    def test_standard_ssh_remote_still_keeps_real_host(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "r", "git@gitlab.com:acme-engineering/secret.git")
        assert _repo_visibility.slug_for_cwd(repo) == "gitlab.com/acme-engineering/secret"

    def test_https_remote_unchanged(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "r", "https://github.com/souliane/teatree.git")
        assert _repo_visibility.slug_for_cwd(repo) == "github.com/souliane/teatree"

    def test_useratalias_ssh_alias_drops_dotless_host_alias(self, tmp_path: Path) -> None:
        # FM2 (#1415): a per-account SSH config Host ALIAS carried WITH a ``user@``
        # part (``git@gh-acct:owner/repo`` from a ``Host gh-acct`` block) was kept
        # verbatim as ``gh-acct/owner/repo`` -- the dotless ``gh-acct`` glued in as
        # the leading slug segment. The downstream dot-keyed host-strip / probe
        # could not recognise it, so the canonical key was wrong. A dotless host
        # is an alias with no canonical identity, so it is dropped -> ``owner/repo``.
        repo = _repo_with_remote(tmp_path / "r", "git@gh-acct:owner-org/private-product.git")
        assert _repo_visibility.slug_for_cwd(repo) == "owner-org/private-product"

    def test_useratalias_dotted_host_alias_resolves_to_owner_repo_after_strip(self, tmp_path: Path) -> None:
        # FM2 (#1415): the exact reported remote shape -- a per-account SSH alias
        # whose name happens to embed the real host with a dot
        # (``git@github.com-acct:Owner/Repo``). The dotted alias is kept as
        # the host segment, but ``_strip_host_prefix`` (and the visibility probe)
        # strip a dotted leading segment, so the canonical owner/repo key resolves.
        repo = _repo_with_remote(tmp_path / "r", "git@github.com-acct:owner-org/private-product.git")
        slug = _repo_visibility.slug_for_cwd(repo)
        assert _repo_visibility._strip_host_prefix(slug) == "owner-org/private-product"

    def test_public_repo_with_alias_host_is_not_downgraded_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end: a PUBLIC repo whose origin uses an alias host containing the
        # allowlist entry must NOT be carved out as private.
        cfg = _config(tmp_path, ["acmecorp"])
        repo = _repo_with_remote(tmp_path / "r", "gitlab-acmecorp:someorg/public-repo.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is False

    def test_useratalias_private_repo_downgrades_via_allowlist_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # FM2 end-to-end (#1415): a known-private own repo cloned through a dotless
        # ``user@alias`` SSH remote must DOWNGRADE via the offline allowlist with
        # no probe tool on PATH. Before the fix the slug carried the ``gh-acct``
        # alias segment, so the ``owner-org`` allowlist entry did not match the
        # leading segment and the own private repo over-blocked.
        cfg = _config(tmp_path, ["owner-org"])
        repo = _repo_with_remote(tmp_path / "r", "git@gh-acct:owner-org/private-product.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert publish_surface.commit_targets_private_repo(repo, config_path=cfg) is True


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
    """Bug 2: the cache must persist even when ``$HOME/.teatree`` is a FILE.

    The historical default rooted the cache at ``$HOME/.teatree`` -- but that
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

    # A chained ``cd <wt> && git add -A && git commit -m …`` is the agent's
    # standard worktree-commit idiom: the ``git commit`` sits in a LATER
    # segment, not the command's first action. It must be recognised as a
    # commit so the private-repo carve-out applies, exactly as the plain
    # ``git commit`` above does (#2215).
    def test_chained_cd_add_commit_downgrades(self, private_cfg: Path, private_repo: Path) -> None:
        body = "fix the acmewidget refinery"
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                f'cd {private_repo} && git add -A && git commit -m "{body}"',
                body,
                private_repo,
                config_path=private_cfg,
            )
            is True
        )

    # SAFETY TEST: a chained commit must NOT vouch for a publish segment in the
    # same chain. A ``cd <wt> && git add && git commit … && gh issue create
    # --repo <PUBLIC>`` carries a foreign term to a PUBLIC repo; the per-segment
    # chain proof must still defeat it even though the commit segment alone
    # would downgrade (#2215, the #2034 per-segment concern).
    def test_chained_commit_then_public_gh_post_stays_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        private_repo = _repo_with_remote(tmp_path / "wt", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")
        # No probe tool -> the public --repo slug is unknown -> NOT private.
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                f'cd {private_repo} && git add -A && git commit -m "acmewidget fix" '
                '&& gh issue create --repo souliane/teatree --title x --body "someforeignbank"',
                "acmewidget fix",
                private_repo,
                config_path=cfg,
            )
            is False
        )

    # SAFETY TEST: This test is load-bearing. A commit whose CUMULATIVE
    # ``-C`` landing dir is a PUBLIC repo must stay hard-blocked even when the
    # bare last ``-C`` segment, resolved alone, would name a private repo.
    # git resolves ``-C /pub -C relpriv`` to ``/pub/relpriv``; the body lands
    # in the PUBLIC repo there, so the carve-out must NOT downgrade.
    def test_cumulative_dash_c_public_landing_stays_hard_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        # Bare last segment ``relpriv`` (resolved vs the process cwd) is PRIVATE.
        _repo_with_remote(tmp_path / "relpriv", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")
        # Cumulative landing ``<pub>/relpriv`` is a PUBLIC/unknown repo.
        pub = tmp_path / "pub"
        _repo_with_remote(pub / "relpriv", "git@github.com:some/unrelated-public.git")
        # No probe tool -> unknown slug stays NOT private.
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        monkeypatch.chdir(tmp_path)
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                f'git -C {pub} -C relpriv commit -m "acmewidget fix"',
                "acmewidget fix",
                None,
                config_path=cfg,
            )
            is False
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

    # The over-block this fix targets: a sub-agent's ``git -C <private>
    # commit`` runs from an ambient hook cwd that has reset away from the
    # worktree. The carve-out must resolve the EFFECTIVE worktree from the
    # command's own ``-C`` flag, not the wrong ambient cwd.
    def test_git_dash_c_private_worktree_downgrades_despite_ambient_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        private_worktree = _repo_with_remote(
            tmp_path / "wt", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        # The ambient hook cwd is an UNRELATED dir with no private origin --
        # simulating the sub-agent shell's reset to ~/workspace.
        ambient_cwd = _repo_with_remote(tmp_path / "ambient", "git@github.com:some/unrelated-repo.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                f'git -C {private_worktree} commit -m "acmewidget refinery"',
                "acmewidget refinery",
                ambient_cwd,
                config_path=cfg,
            )
            is True
        )

    # SAFETY TEST: ``git -C <public-worktree> commit`` must STAY hard-blocked.
    # The resolver reads the real dir's origin (public), so the carve-out does
    # not apply -- widening must never let a public-repo commit downgrade.
    def test_git_dash_c_public_worktree_stays_hard_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        public_worktree = _repo_with_remote(tmp_path / "wt", "git@github.com:souliane/teatree.git")
        ambient_cwd = _repo_with_remote(
            tmp_path / "ambient", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        # No probe tool -> the public worktree's slug is unknown -> NOT private.
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                f'git -C {public_worktree} commit -m "acmewidget"',
                "acmewidget",
                ambient_cwd,
                config_path=cfg,
            )
            is False
        )

    # A ``-C`` dir that is not inside ANY git repo is a genuinely-unresolvable
    # LOCAL commit: git itself rejects a commit outside a repo, so it cannot
    # leak. The commit BODY fails OPEN (downgrade), never over-blocking a
    # legitimate local commit -- distinct from a resolvable-PUBLIC target,
    # which still hard-blocks (see the ``-C <public repo>`` test below).
    def test_git_dash_c_dir_not_in_any_repo_fails_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        ambient_cwd = _repo_with_remote(
            tmp_path / "ambient", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                'git -C /nonexistent/worktree commit -m "acmewidget"',
                "acmewidget",
                ambient_cwd,
                config_path=cfg,
            )
            is True
        )

    # SAFETY TEST: a ``-C`` dir that RESOLVES to a PUBLIC repo stays
    # hard-blocked. Fail-open is ONLY for a dir inside no repo at all, never
    # for a resolvable-public target.
    def test_git_dash_c_public_repo_stays_hard_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        public_worktree = _repo_with_remote(tmp_path / "wt", "git@github.com:souliane/teatree.git")
        ambient_cwd = _repo_with_remote(
            tmp_path / "ambient", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                f'git -C {public_worktree} commit -m "acmewidget"',
                "acmewidget",
                ambient_cwd,
                config_path=cfg,
            )
            is False
        )

    def test_plain_git_commit_in_private_cwd_still_downgrades(self, private_cfg: Path, private_repo: Path) -> None:
        # No -C flag -> the cwd fallback is used unchanged. A plain ``git
        # commit`` in a private worktree cwd still downgrades (no regression).
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                'git commit -m "acmewidget refinery"',
                "acmewidget refinery",
                private_repo,
                config_path=private_cfg,
            )
            is True
        )

    def test_git_dash_c_secret_in_body_stays_hard_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A secret hard-blocks regardless of repo privacy or the -C target.
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        private_worktree = _repo_with_remote(
            tmp_path / "wt", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        ambient_cwd = _repo_with_remote(tmp_path / "ambient", "git@github.com:some/unrelated-repo.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        body = "token is ghp_" + "a" * 36
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                f"git -C {private_worktree} commit -F /tmp/msg",
                body,
                ambient_cwd,
                config_path=cfg,
            )
            is False
        )

    # SAFETY TEST (the leak): ``--git-dir <PUBLIC> --work-tree <PRIVATE>``.
    # The commit LANDS in the public git-dir; ``--work-tree`` only sets the
    # working tree and never selects the repo. So the carve-out MUST NOT
    # apply -- the banned term would egress to public history. Treating the
    # three flags as interchangeable last-wins resolves the private work-tree
    # and downgrades, which is the leak this fix closes.
    def test_public_git_dir_private_work_tree_stays_hard_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        public_git_dir = _repo_with_remote(tmp_path / "pub", "git@github.com:souliane/teatree.git")
        private_work_tree = _repo_with_remote(
            tmp_path / "priv", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        ambient_cwd = _repo_with_remote(tmp_path / "ambient", "git@github.com:some/unrelated-repo.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                f'git --git-dir={public_git_dir}/.git --work-tree={private_work_tree} commit -m "acmewidget refinery"',
                "acmewidget refinery",
                ambient_cwd,
                config_path=cfg,
            )
            is False
        )

    # SAFETY TEST (order-independence): the same leak with ``--work-tree``
    # BEFORE ``--git-dir`` must ALSO stay hard-blocked. Proves the fix is a
    # model fix (repo identity comes from git-dir/-C only) not an ordering
    # patch that depends on which flag appears last.
    def test_private_work_tree_before_public_git_dir_stays_hard_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        public_git_dir = _repo_with_remote(tmp_path / "pub", "git@github.com:souliane/teatree.git")
        private_work_tree = _repo_with_remote(
            tmp_path / "priv", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        ambient_cwd = _repo_with_remote(tmp_path / "ambient", "git@github.com:some/unrelated-repo.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                f'git --work-tree={private_work_tree} --git-dir={public_git_dir}/.git commit -m "acmewidget refinery"',
                "acmewidget refinery",
                ambient_cwd,
                config_path=cfg,
            )
            is False
        )

    # The commit lands in the PRIVATE git-dir, so the carve-out correctly
    # applies despite a PUBLIC work-tree -- ``--work-tree`` never selects the
    # repo, and the git-dir's origin is what governs the privacy decision.
    def test_private_git_dir_public_work_tree_downgrades(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        private_git_dir = _repo_with_remote(
            tmp_path / "priv", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        public_work_tree = _repo_with_remote(tmp_path / "pub", "git@github.com:souliane/teatree.git")
        ambient_cwd = _repo_with_remote(tmp_path / "ambient", "git@github.com:some/unrelated-repo.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                f'git --git-dir={private_git_dir}/.git --work-tree={public_work_tree} commit -m "acmewidget refinery"',
                "acmewidget refinery",
                ambient_cwd,
                config_path=cfg,
            )
            is True
        )

    # REGRESSION (#1415): a sub-agent's ``git -C <RELATIVE worktree>`` commit.
    # The ambient hook cwd has reset away from the worktree to a SIBLING repo,
    # and the in-command ``-C`` value is RELATIVE to that ambient cwd -- the
    # form a worktree path takes when written relative to the workspace root.
    # A relative target must be anchored on the AMBIENT cwd, not the cold
    # hook's process cwd (which is outside any repo). Without the anchor, the
    # relative ``-C`` resolves to a path in no git repo and the carve-out
    # FAIL-OPENS for the wrong reason; here the resolved repo is allowlisted
    # PRIVATE, so the carve-out must apply (downgrade) deterministically.
    def test_relative_dash_c_private_target_anchored_on_ambient_cwd_downgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        # The ambient hook cwd is a SIBLING repo (public/unrelated), NOT the
        # private worktree the relative ``-C`` names.
        ambient_cwd = _repo_with_remote(workspace / "public-sibling", "git@github.com:some/public-sibling.git")
        # The real target worktree (resolved relative to the AMBIENT cwd) is a
        # private allowlisted repo, a sibling under workspace.
        _repo_with_remote(workspace / "priv-worktree", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")
        # DECOY at the PROCESS-cwd-relative location: a PUBLIC repo at the same
        # ``../priv-worktree`` relative path. If the gate (wrongly) anchored the
        # relative ``-C`` on the process cwd, it would resolve HERE -> public ->
        # hard-block, so this test fails unless the anchor uses the AMBIENT cwd.
        process_cwd = tmp_path / "process" / "cwd"
        process_cwd.mkdir(parents=True)
        _repo_with_remote(tmp_path / "process" / "priv-worktree", "git@github.com:souliane/teatree.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        monkeypatch.chdir(process_cwd)
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                'git -C ../priv-worktree commit -m "acmewidget refinery"',
                "acmewidget refinery",
                ambient_cwd,
                config_path=cfg,
            )
            is True
        )

    # REGRESSION (#1415) -- the load-bearing LEAK direction. The ambient hook
    # cwd is an allowlisted PRIVATE repo, but the in-command relative ``-C``
    # names a PUBLIC sibling worktree the commit actually lands in. Anchoring
    # the relative target on the ambient cwd resolves it to the PUBLIC repo, so
    # the carve-out must NOT apply -- the banned-term commit to the public repo
    # stays HARD-BLOCKED. (Before the anchor, the relative path resolved to no
    # repo at the process cwd and FAIL-OPENED, allowing a public leak.)
    def test_relative_dash_c_public_target_from_private_ambient_cwd_stays_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        # Ambient cwd is a PRIVATE allowlisted repo.
        ambient_cwd = _repo_with_remote(
            workspace / "priv-sibling", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        # The real target worktree is a PUBLIC sibling under workspace.
        _repo_with_remote(workspace / "public-worktree", "git@github.com:souliane/teatree.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        (tmp_path / "non-git-process-cwd").mkdir()
        monkeypatch.chdir(tmp_path / "non-git-process-cwd")
        assert (
            publish_surface.carve_out_applies(
                "Bash",
                'git -C ../public-worktree commit -m "acmewidget refinery"',
                "acmewidget refinery",
                ambient_cwd,
                config_path=cfg,
            )
            is False
        )


class TestOwnSlugTermDowngrades:
    """A commit tripping on its OWN repo-slug term (#126 follow-up).

    A work-item URL in a commit message (``host/<org>/<repo>/-/issues/N``) is
    the repo naming itself, not a foreign customer leak. When the matched term
    is a ``[teatree] private_repos`` allowlist entry AND the commit lands in
    that private repo (or a genuinely-unresolvable LOCAL commit), it warns. A
    foreign customer term, or a resolvable PUBLIC landing repo, stays blocked.
    """

    @pytest.fixture
    def cfg(self, tmp_path: Path) -> Path:
        return _config(tmp_path, ["acmecorp-engineering"])

    @pytest.fixture
    def private_repo(self, tmp_path: Path) -> Path:
        return _repo_with_remote(tmp_path / "r", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git")

    # (a) Own-org slug term on the repo's own private commit -> downgrade,
    # even when the bare commit's cwd resolution would otherwise miss it.
    def test_own_org_slug_term_in_private_repo_downgrades(self, cfg: Path, private_repo: Path) -> None:
        assert (
            publish_surface.own_slug_term_downgrades(
                'git commit -m "feat: work item https://gitlab.com/acmecorp-engineering/acmecorp-product/-/issues/42"',
                "acmecorp-engineering",
                private_repo,
                config_path=cfg,
            )
            is True
        )

    # (a) The own-slug downgrade must also fire on the chained worktree-commit
    # idiom ``cd <wt> && git add -A && git commit -m …`` where the ``git
    # commit`` sits in a LATER segment, not the command's first action. Without
    # per-segment recognition the chained commit is not seen as a commit and a
    # legitimate own-slug commit is hard-blocked as if it were a publish (#2215).
    def test_own_org_slug_term_chained_cd_add_commit_downgrades(self, cfg: Path, private_repo: Path) -> None:
        assert (
            publish_surface.own_slug_term_downgrades(
                f"cd {private_repo} && git add -A "
                '&& git commit -m "ref https://gitlab.com/acmecorp-engineering/acmecorp-product/-/issues/7"',
                "acmecorp-engineering",
                private_repo,
                config_path=cfg,
            )
            is True
        )

    # (b) SAFETY: the chained own-slug commit must NOT vouch for a chained
    # PUBLIC gh post. The per-segment chain proof must still defeat a
    # ``cd <wt> && git add && git commit … && gh issue create --repo <PUBLIC>``
    # so the chained-commit recognition never weakens the gate (#2215, #2034).
    def test_chained_own_slug_commit_then_public_gh_post_stays_blocked(
        self, tmp_path: Path, cfg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        private_worktree = _repo_with_remote(
            tmp_path / "wt", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.own_slug_term_downgrades(
                f'cd {private_worktree} && git add -A && git commit -m "acmecorp-engineering ref" '
                '&& gh issue create --repo souliane/teatree --title x --body "someforeignbank"',
                "acmecorp-engineering",
                private_worktree,
                config_path=cfg,
            )
            is False
        )

    # (a) An unresolvable LOCAL commit (cwd not in any repo) tripping on its
    # own org slug fails OPEN -- it cannot leak. Mirrors the body fail-open.
    def test_own_org_slug_term_unresolvable_local_commit_downgrades(
        self, tmp_path: Path, cfg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        assert (
            publish_surface.own_slug_term_downgrades(
                'git commit -m "ref https://gitlab.com/acmecorp-engineering/acmecorp-product/-/issues/1"',
                "acmecorp-engineering",
                non_repo,
                config_path=cfg,
            )
            is True
        )

    # (b) SAFETY: the SAME own-org slug term on a PUBLIC repo commit stays
    # hard-blocked -- the carve-out is repo-scoped, never term-scoped alone.
    def test_own_org_slug_term_in_public_repo_stays_blocked(
        self, tmp_path: Path, cfg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        public_repo = _repo_with_remote(tmp_path / "pub", "git@github.com:someorg/public-thing.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.own_slug_term_downgrades(
                'git commit -m "ref https://gitlab.com/acmecorp-engineering/acmecorp-product/-/issues/1"',
                "acmecorp-engineering",
                public_repo,
                config_path=cfg,
            )
            is False
        )

    # (b) SAFETY: a FOREIGN customer term is not an allowlist entry, so it
    # never qualifies for the own-slug downgrade even on a private repo
    # (its real eligibility, if any, is decided by the primary carve-out).
    def test_foreign_customer_term_does_not_qualify(self, cfg: Path, private_repo: Path) -> None:
        assert (
            publish_surface.own_slug_term_downgrades(
                'git commit -m "fix for someforeignbank"',
                "someforeignbank",
                private_repo,
                config_path=cfg,
            )
            is False
        )

    # A foreign term that merely CONTAINS an allowlist substring must not
    # qualify: matching is token-equality, not substring.
    def test_substring_of_allowlist_entry_does_not_qualify(self, cfg: Path, private_repo: Path) -> None:
        assert (
            publish_surface.own_slug_term_downgrades(
                'git commit -m "acmecorp-engineering-services audit"',
                "acmecorp-engineering-services",
                private_repo,
                config_path=cfg,
            )
            is False
        )

    # The downgrade is git-commit-only: a gh/glab post never qualifies for
    # the own-slug path (the posting path has its own pure-private proof).
    def test_gh_post_does_not_qualify(self, cfg: Path, private_repo: Path) -> None:
        assert (
            publish_surface.own_slug_term_downgrades(
                "gh pr create --repo acmecorp-engineering/acmecorp-product --title x",
                "acmecorp-engineering",
                private_repo,
                config_path=cfg,
            )
            is False
        )

    # (c) Worktree-gitdir regression: ``git -C <private-worktree> commit`` whose
    # ambient cwd reset to an unrelated repo still downgrades on its own slug.
    def test_dash_c_private_worktree_own_slug_downgrades_despite_ambient_cwd(
        self, tmp_path: Path, cfg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        private_worktree = _repo_with_remote(
            tmp_path / "wt", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        ambient_cwd = _repo_with_remote(tmp_path / "ambient", "git@github.com:some/unrelated-repo.git")
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.own_slug_term_downgrades(
                f'git -C {private_worktree} commit -m "ref .../acmecorp-engineering/..."',
                "acmecorp-engineering",
                ambient_cwd,
                config_path=cfg,
            )
            is True
        )

    # BLOCKER (security): a private commit on the OWN slug term chained to a
    # PUBLIC gh post must NOT downgrade. is_git_commit_command matches the FIRST
    # segment and the scanner reports the FIRST matched term, so without the
    # per-segment chain proof an own-slug commit would WARN and the chained
    # `gh issue create --repo <PUBLIC>` (carrying a foreign customer term in its
    # body) would publish to a public repo. The chain proof must defeat it.
    def test_own_slug_commit_chained_to_public_gh_post_stays_blocked(
        self, tmp_path: Path, cfg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        private_worktree = _repo_with_remote(
            tmp_path / "wt", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        # No probe tool -> the public --repo slug is unknown -> NOT private.
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.own_slug_term_downgrades(
                f'git -C {private_worktree} commit -m "acmecorp-engineering ref" '
                '&& gh issue create --repo souliane/teatree --title x --body "someforeignbank"',
                "acmecorp-engineering",
                private_worktree,
                config_path=cfg,
            )
            is False
        )

    # A chained segment carrying a forge tool inside a quoted shell string
    # (`sh -c "gh ... PUBLIC"`) is NOT publish-inert -> stays blocked.
    def test_own_slug_commit_chained_to_shell_wrapped_forge_stays_blocked(
        self, tmp_path: Path, cfg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        private_worktree = _repo_with_remote(
            tmp_path / "wt", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.own_slug_term_downgrades(
                f'git -C {private_worktree} commit -m "acmecorp-engineering" '
                '&& sh -c "gh issue create --repo souliane/teatree --body x"',
                "acmecorp-engineering",
                private_worktree,
                config_path=cfg,
            )
            is False
        )

    # A chained PUBLISH-INERT segment (git push, echo) preserves the downgrade:
    # the own-slug commit still warns because nothing in the chain can publish.
    def test_own_slug_commit_chained_to_inert_segment_downgrades(
        self, tmp_path: Path, cfg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        private_worktree = _repo_with_remote(
            tmp_path / "wt", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.own_slug_term_downgrades(
                f'git -C {private_worktree} commit -m "acmecorp-engineering ref" && git push origin HEAD',
                "acmecorp-engineering",
                private_worktree,
                config_path=cfg,
            )
            is True
        )

    # A chained PURE PRIVATE gh post preserves the downgrade: the chained
    # segment posts to a known-private repo, so it cannot leak the own-slug term.
    def test_own_slug_commit_chained_to_private_gh_post_downgrades(
        self, tmp_path: Path, cfg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        private_worktree = _repo_with_remote(
            tmp_path / "wt", "git@gitlab.com:acmecorp-engineering/acmecorp-product.git"
        )
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "bin"))
        assert (
            publish_surface.own_slug_term_downgrades(
                f'git -C {private_worktree} commit -m "acmecorp-engineering ref" '
                "&& gh issue create --repo acmecorp-engineering/acmecorp-product --title x",
                "acmecorp-engineering",
                private_worktree,
                config_path=cfg,
            )
            is True
        )


class TestTermIsOwnRepoSlug:
    """``_repo_visibility.term_is_own_repo_slug`` token-containment contract."""

    def test_exact_allowlist_entry_is_own_slug(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        assert _repo_visibility.term_is_own_repo_slug("acmecorp-engineering", cfg) is True

    def test_token_equal_spelling_is_own_slug(self, tmp_path: Path) -> None:
        # ``acmecorp_engineering`` tokenizes to the same tokens as the entry.
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        assert _repo_visibility.term_is_own_repo_slug("acmecorp_engineering", cfg) is True

    # #1958: the org PREFIX token of a multi-token slug is the repo's own
    # identity -- the scanner reports that token tokenized out of a work-item URL.
    def test_org_prefix_token_of_entry_is_own_slug(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        assert _repo_visibility.term_is_own_repo_slug("acmecorp", cfg) is True

    # A non-leading token-run within the entry also qualifies (it is still the
    # repo's own identity, never a foreign term).
    def test_inner_token_of_entry_is_own_slug(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        assert _repo_visibility.term_is_own_repo_slug("engineering", cfg) is True

    def test_foreign_term_is_not_own_slug(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        assert _repo_visibility.term_is_own_repo_slug("someforeignbank", cfg) is False

    # A token that is NOT part of any entry's token run stays foreign even when
    # it shares a leading character run with an entry token (no false prefix).
    def test_unrelated_partial_token_is_not_own_slug(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        assert _repo_visibility.term_is_own_repo_slug("acme", cfg) is False

    def test_superset_term_is_not_own_slug(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["acmecorp-engineering"])
        assert _repo_visibility.term_is_own_repo_slug("acmecorp-engineering-services", cfg) is False

    def test_empty_allowlist_never_matches(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, [])
        assert _repo_visibility.term_is_own_repo_slug("acmecorp-engineering", cfg) is False


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


# must-ALLOW: a private-target post/commit PROVES pure and downgrades to warn.
# These are the over-block dimension of the prove-pure inversion -- every legit
# private-post shape the factory and user actually use (prefixed / env / cd-
# prefixed posting verbs, a private READ chained after a post, a flag VALUE
# whose prose contains the word ``gh``/``glab`` or a quoted ``sh -c`` string)
# must still downgrade, else the allowlist over-blocks legitimate work.
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
)

# must-DENY: the load-bearing leak guards -- anything the prove-pure proof
# cannot prove is a pure private post stays hard-blocked. A public/unknown
# target, a raw-REST segment, a secret, a chained public posting segment, the
# commit-plus-public-post guard, ANY execution-transport construct (subshell,
# process substitution, shell ``-c``, ``env -S``, here-string/here-doc,
# ``eval``, pipe-to-shell, a ``$()`` inside a flag value), a chained non-``gh``
# command (even a benign-looking one), and NOVEL transports never enumerated by
# any prior cycle (``ssh``/``node -e``/``make``/``source <(...)``/...) must ALL
# fail the proof -- transport-agnostically, by not being a recognised pure post.
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
    # L1-L13: a PUBLIC-targeting post wrapped in a subshell / process
    # substitution / brace group / wrapper word. Under the prove-pure inversion
    # the wrapping segment's ``words[0]`` is the opener or wrapper word, not an
    # EXACT ``gh``/``glab``, so the segment is not a recognised pure post and the
    # whole command fails the proof -- no enumeration of the wrapper needed.
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
    # S1-S4: a public ``gh`` post buried in a quoted shell ``-c`` argument.
    # Under the prove-pure inversion the ``sh``/``bash``/``zsh`` segment's
    # ``words[0]`` is the shell, not an EXACT ``gh``/``glab``, so the segment is
    # not a recognised pure post and the proof fails closed -- the inner verb is
    # never inspected and no shell needs to be enumerated.
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
    # S5-S11: a wrapper word (``timeout``/``nice``/``xargs``/...), a path-form
    # shell (``/bin/sh``, ``/usr/bin/env bash``), the ``find -exec sh -c ... \;``
    # form, and a nested ``sh -c "sh -c 'gh ...'"`` all chain a shell whose
    # ``words[0]`` is not an EXACT ``gh``/``glab``. The prove-pure proof rejects
    # each for not being a recognised pure post -- no shell-basename scan, no
    # ``-c``-argument recursion, no enumeration.
    _CorpusRow(
        "S5",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& timeout 5 sh -c "gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "S6",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& nice sh -c "gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "S7",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& /bin/sh -c "gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "S8",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& /usr/bin/env bash -c "gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "S9",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'| xargs sh -c "gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "S10",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& find . -exec sh -c "gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}" \\;',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "S11",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f"&& sh -c \"sh -c 'gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}'\"",
        _TERM,
        _PRIV_REMOTE,
    ),
    # S12-S15: ``env -S`` / ``env --split-string``, a here-string ``<shell>
    # <<<``, and ``eval`` each chain a segment whose ``words[0]`` is the
    # introducer command (``env``/``bash``/``eval``), not an EXACT
    # ``gh``/``glab``. The prove-pure proof rejects each for not being a
    # recognised pure post -- no per-introducer operand extractor, no registry,
    # no recursion.
    _CorpusRow(
        "S12",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f"&& env -S \"sh -c 'gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}'\"",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "S13",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& env -S "gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "S13b",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& env --split-string="gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "S14",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f'&& bash <<< "gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "S15",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f"&& eval \"sh -c 'gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}'\"",
        _TERM,
        _PRIV_REMOTE,
    ),
    # S16: a here-doc whose literal body is a public ``gh`` invocation. The lexer
    # splits the body at its newlines into its OWN segment, so the public-target
    # post becomes a recognised posting segment whose target check fails closed.
    _CorpusRow(
        "S16",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f"&& bash << EOF\ngh issue create --repo {_PUBLIC_SLUG} --body {_TERM}\nEOF",
        _TERM,
        _PRIV_REMOTE,
    ),
    # P1-P3: under the prove-pure inversion a private post CHAINED with any
    # non-``gh`` command -- even a benign-looking ``sh -c "date"``, an ``eval``
    # with an inert string, or a bare ``echo`` -- hard-blocks. Proving the
    # chained command inert requires inspecting the shell-string, which is the
    # very denylist the inversion retires; so the proof fails closed. This is
    # the safe, recoverable price (split into a plain post), and it makes the
    # previously-"accepted runtime residual" A13 a correct hard-block.
    _CorpusRow(
        "P1",
        f'gh issue create --repo {_PRIV_SLUG} --body ok && sh -c "date"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "P2",
        f'gh issue create --repo {_PRIV_SLUG} --body ok && eval "echo done"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "P3",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && echo done",
        _TERM,
        _PRIV_REMOTE,
    ),
    # N1-N6: NOVEL transports never enumerated by any prior cycle. The inversion
    # is transport-agnostic -- each is rejected for not being part of a
    # recognised pure ``gh``/``glab`` post, not because it was added to a list.
    _CorpusRow(
        "N1",
        f'gh issue create --repo {_PRIV_SLUG} --body ok && ssh localhost "gh issue create --repo {_PUBLIC_SLUG}"',
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "N2",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f"&& node -e \"require('child_process').execSync('gh issue create --repo {_PUBLIC_SLUG}')\"",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "N3",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && make publish-public-post",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "N4",
        f"gh issue create --repo {_PRIV_SLUG} --body ok && source <(echo gh issue create --repo {_PUBLIC_SLUG})",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "N5",
        f"gh issue create --repo {_PRIV_SLUG} --body ok "
        f"&& python3 -c \"import os; os.system('gh issue create --repo {_PUBLIC_SLUG}')\"",
        _TERM,
        _PRIV_REMOTE,
    ),
    _CorpusRow(
        "N6",
        f'gh issue create --repo {_PRIV_SLUG} --body "$(gh issue create --repo {_PUBLIC_SLUG} --body {_TERM})"',
        _TERM,
        _PRIV_REMOTE,
    ),
)


class TestCarveOutGoldenCorpus:
    """HERMETIC golden must-ALLOW / must-DENY corpus for the prove-pure carve-out.

    The binding durable artifact for the two dimensions of the inversion: the
    must-ALLOW set is the OVER-BLOCK guard (every legit private-post shape still
    downgrades) and the must-DENY set is the LEAK guard (anything not provably a
    pure private post -- any transport, public target, raw REST, secret, novel
    mechanism -- hard-blocks). Fully offline: ``gh``/``glab`` are ABSENT from
    PATH and ``_PROBE_PATH_EXTRA`` is emptied, so any non-allowlisted slug
    resolves NOT-private deterministically (no network). The PRIVATE namespace
    is injected into the tmp allowlist; the PUBLIC slug is never allowlisted.
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


def _proves_pure(command: str, tmp_path: Path) -> bool:
    """Run ``command_is_pure_private_gh_glab_post`` against a hermetic offline env.

    The PRIVATE namespace is allowlisted; the PUBLIC slug never is, and no
    probe tool is reachable, so any non-allowlisted slug resolves NOT-private
    deterministically. The CWD origin is the PRIVATE remote, so a flagless
    private post still downgrades via the CWD fallback.
    """
    cfg = _config(tmp_path, [_PRIV_NS])
    cwd = _repo_with_remote(tmp_path / "cwd", _PRIV_REMOTE)
    return publish_surface.command_is_pure_private_gh_glab_post(command, cwd, config_path=cfg)


class TestPurityProofBlocksTransportLeaks:
    """The transport leaks the OLD denylist enumerated now fail the purity proof.

    Each scenario chains a real public post behind a transport construct
    (shell ``-c``, ``env -S``, here-string, ``eval``, wrapper word, path-form
    shell, ``find -exec``, pipe-to-shell, nesting). Under the inversion the
    proof does not try to DETECT the hidden public post -- the segment carrying
    the transport is simply not a recognised pure ``gh``/``glab`` post, so the
    whole command fails the proof and the hard-block stands. The proof is
    transport-agnostic: it never enumerates ``sh``/``bash``/``eval``/...; it
    rejects them for not being part of a recognised post.
    """

    @pytest.fixture(autouse=True)
    def _offline_probe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "probebin"))
        monkeypatch.setattr(_repo_visibility, "_PROBE_PATH_EXTRA", ())
        monkeypatch.delenv("GH_REPO", raising=False)
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

    @pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash", "ksh", "ash"])
    @pytest.mark.parametrize("flag", ["-c", "-lc", "-ic", "-xc"])
    def test_shell_c_transport_blocks(self, shell: str, flag: str, tmp_path: Path) -> None:
        cmd = f'gh issue create --repo {_PRIV_SLUG} --body ok && {shell} {flag} "gh issue create --repo {_PUBLIC_SLUG}"'
        assert _proves_pure(cmd, tmp_path) is False

    @pytest.mark.parametrize(
        "introducer",
        [
            f'env -S "gh issue create --repo {_PUBLIC_SLUG}"',
            f'env --split-string="gh issue create --repo {_PUBLIC_SLUG}"',
            f'bash <<< "gh issue create --repo {_PUBLIC_SLUG}"',
            f'eval "gh issue create --repo {_PUBLIC_SLUG}"',
            f"eval gh issue create --repo {_PUBLIC_SLUG}",
            f'timeout 5 sh -c "gh issue create --repo {_PUBLIC_SLUG}"',
            f'/bin/sh -c "gh issue create --repo {_PUBLIC_SLUG}"',
            f'/usr/bin/env bash -c "gh issue create --repo {_PUBLIC_SLUG}"',
            f'find . -exec sh -c "gh issue create --repo {_PUBLIC_SLUG}" \\;',
            f"sh -c \"sh -c 'gh issue create --repo {_PUBLIC_SLUG}'\"",
        ],
    )
    def test_introducer_transport_blocks(self, introducer: str, tmp_path: Path) -> None:
        cmd = f"gh issue create --repo {_PRIV_SLUG} --body ok && {introducer}"
        assert _proves_pure(cmd, tmp_path) is False

    def test_pipe_to_shell_transport_blocks(self, tmp_path: Path) -> None:
        cmd = f"gh issue create --repo {_PRIV_SLUG} --body ok | xargs gh issue create --repo {_PUBLIC_SLUG}"
        assert _proves_pure(cmd, tmp_path) is False

    @pytest.mark.parametrize(
        "wrapper",
        [
            f"( gh issue create --repo {_PUBLIC_SLUG} )",
            f"$( gh issue create --repo {_PUBLIC_SLUG} )",
            f"{{ gh issue create --repo {_PUBLIC_SLUG}; }}",
            f"env FOO=x gh issue create --repo {_PUBLIC_SLUG}",
            f"command gh issue create --repo {_PUBLIC_SLUG}",
        ],
    )
    def test_wrapper_word_or_group_blocks(self, wrapper: str, tmp_path: Path) -> None:
        cmd = f"gh issue create --repo {_PRIV_SLUG} --body ok && {wrapper}"
        assert _proves_pure(cmd, tmp_path) is False

    def test_substitution_in_body_value_blocks(self, tmp_path: Path) -> None:
        # ``--body "$(gh ... PUBLIC ...)"`` -- a command substitution inside a
        # flag value runs a second post at runtime, so the value is not provably
        # inert: the proof rejects any token carrying a ``$(`` marker.
        cmd = f'gh issue create --repo {_PRIV_SLUG} --body "$(gh issue create --repo {_PUBLIC_SLUG})"'
        assert _proves_pure(cmd, tmp_path) is False


class TestPurityProofBlocksNovelTransports:
    """Transports NEVER enumerated by any prior cycle still block.

    The inversion is transport-agnostic: ``ssh``, ``node -e``, ``make``, a
    ``source <(...)`` process substitution -- none were ever in a denylist, yet
    each is rejected for not being part of a recognised pure ``gh``/``glab``
    post. This is the load-bearing proof that the model closed the
    whack-a-mole rather than adding three more entries to it.
    """

    @pytest.fixture(autouse=True)
    def _offline_probe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "probebin"))
        monkeypatch.setattr(_repo_visibility, "_PROBE_PATH_EXTRA", ())
        monkeypatch.delenv("GH_REPO", raising=False)
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

    @pytest.mark.parametrize(
        "transport",
        [
            f'ssh localhost "gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}"',
            f"node -e \"require('child_process').execSync('gh issue create --repo {_PUBLIC_SLUG}')\"",
            f"python3 -c \"import os; os.system('gh issue create --repo {_PUBLIC_SLUG}')\"",
            "make publish-public-post",
            f"source <(echo gh issue create --repo {_PUBLIC_SLUG})",
            f'perl -e "system(q{{gh issue create --repo {_PUBLIC_SLUG}}})"',
        ],
    )
    def test_novel_transport_blocks(self, transport: str, tmp_path: Path) -> None:
        cmd = f"gh issue create --repo {_PRIV_SLUG} --body ok && {transport}"
        assert _proves_pure(cmd, tmp_path) is False


class TestPurityProofAllowsPrivatePosts:
    """Legit private-post shapes still PROVE pure -- the over-block dimension.

    The inverted proof MUST keep downgrading every private-post shape the
    factory and user actually use, else it over-blocks legitimate work. A flag
    VALUE containing the word ``gh``/``glab`` or a quoted ``sh -c ...`` STRING
    is opaque prose, not a second command, so it stays pure and downgrades.
    """

    @pytest.fixture(autouse=True)
    def _offline_probe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "probebin"))
        monkeypatch.setattr(_repo_visibility, "_PROBE_PATH_EXTRA", ())
        monkeypatch.delenv("GH_REPO", raising=False)
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

    @pytest.mark.parametrize(
        "command",
        [
            f"gh issue create --repo {_PRIV_SLUG} --body x",
            f"gh pr create --repo {_PRIV_SLUG} --title x --body y --label bug",
            f"gh issue comment 5 --repo {_PRIV_SLUG} --body x",
            f"glab mr create --repo {_PRIV_SLUG} --title x --description y",
            f"glab issue create --repo {_PRIV_SLUG} --title x",
            f"cd /some/worktree && gh issue create --repo {_PRIV_SLUG} --body x",
            f"GH_TOKEN=x gh issue create --repo {_PRIV_SLUG} --body x",
            f"gh issue create --repo {_PRIV_SLUG} --body x && gh pr comment 5 --repo {_PRIV_SLUG} --body y",
            f'gh issue create --repo {_PRIV_SLUG} --body "run gh issue list later for glab notes"',
            f'gh issue create --repo {_PRIV_SLUG} --body "see (gh issue 5) and glab notes here"',
            f"""gh issue create --repo {_PRIV_SLUG} --body "later run sh -c 'date' for the build\"""",
        ],
    )
    def test_legit_private_post_proves_pure(self, command: str, tmp_path: Path) -> None:
        assert _proves_pure(command, tmp_path) is True

    def test_flagless_private_cwd_proves_pure(self, tmp_path: Path) -> None:
        assert _proves_pure(f"gh issue create --body {_TERM}", tmp_path) is True


class TestPurityProofStructuralPrimitives:
    """The visibility-independent token classifier in ``_gh_glab_hiding``."""

    def test_plain_private_post_is_structurally_pure(self) -> None:
        words = ["gh", "issue", "create", "--repo", _PRIV_SLUG, "--body", "x"]
        assert _gh_glab_hiding.segment_is_pure_gh_glab_post(words) is True

    def test_cd_and_env_prefix_is_stripped(self) -> None:
        words = ["cd", "/x", "gh", "pr", "create", "--repo", _PRIV_SLUG, "--title", "x"]
        assert _gh_glab_hiding.segment_is_pure_gh_glab_post(words) is True

    def test_substitution_marker_in_value_is_not_pure(self) -> None:
        words = ["gh", "issue", "create", "--repo", _PRIV_SLUG, "--body", f"$(gh issue create --repo {_PUBLIC_SLUG})"]
        assert _gh_glab_hiding.segment_is_pure_gh_glab_post(words) is False

    def test_backtick_marker_in_value_is_not_pure(self) -> None:
        words = ["gh", "issue", "create", "--repo", _PRIV_SLUG, "--body", "`gh issue create`"]
        assert _gh_glab_hiding.segment_is_pure_gh_glab_post(words) is False

    def test_prose_paren_in_value_stays_pure(self) -> None:
        words = ["gh", "issue", "create", "--repo", _PRIV_SLUG, "--body", "see (gh issue 5)"]
        assert _gh_glab_hiding.segment_is_pure_gh_glab_post(words) is True

    def test_redirection_token_is_not_pure(self) -> None:
        words = ["gh", "issue", "create", "--repo", _PRIV_SLUG, ">/tmp/out"]
        assert _gh_glab_hiding.segment_is_pure_gh_glab_post(words) is False

    def test_group_opener_token_is_not_pure(self) -> None:
        words = ["gh", "issue", "create", "--repo", _PRIV_SLUG, "{"]
        assert _gh_glab_hiding.segment_is_pure_gh_glab_post(words) is False

    def test_non_gh_words0_is_not_pure(self) -> None:
        assert _gh_glab_hiding.segment_is_pure_gh_glab_post(["sh", "-c", "gh issue create"]) is False

    def test_path_form_gh_is_not_pure(self) -> None:
        words = ["/usr/bin/gh", "issue", "create", "--repo", _PRIV_SLUG]
        assert _gh_glab_hiding.segment_is_pure_gh_glab_post(words) is False

    def test_too_short_is_not_pure(self) -> None:
        assert _gh_glab_hiding.segment_is_pure_gh_glab_post(["gh", "pr"]) is False

    def test_malformed_cd_prefix_is_not_pure(self) -> None:
        assert _gh_glab_hiding.strip_benign_prefix(["cd"]) is None

    def test_publish_inert_segment_for_commit_chain(self) -> None:
        assert publish_surface._segment_is_publish_inert(["git", "push", "origin", "main"]) is True
        assert publish_surface._segment_is_publish_inert(["echo", "done"]) is True

    def test_forge_or_transport_segment_is_not_publish_inert(self) -> None:
        assert publish_surface._segment_is_publish_inert(["sh", "-c", "gh issue create"]) is False
        assert publish_surface._segment_is_publish_inert(["gh", "issue", "create"]) is False
        assert publish_surface._segment_is_publish_inert(["echo", "$(gh issue create)"]) is False


class TestPurityProofIsTheDecisionPath:
    """Meta-test: the carve-out decides via the prove-pure ALLOWLIST predicate.

    The durable anti-whack-a-mole contract. The carve-out's posting-path
    decision is ``command_is_pure_private_gh_glab_post`` -- a single positive
    proof that the WHOLE command is good -- NOT an enumerated set of execution
    introducers (``_EXEC_INTRODUCERS``) the gate tries to DETECT. The six prior
    cycles all failed by growing that enumeration; this test pins that the
    enumeration is gone and the decision path is the allowlist.
    """

    def test_no_exec_introducer_enumeration_remains(self) -> None:
        # The denylist registry that leaked on every un-enumerated transport is
        # retired; its absence is the structural signal the model inverted.
        assert not hasattr(_gh_glab_hiding, "_EXEC_INTRODUCERS")
        assert not hasattr(_gh_glab_hiding, "_EXEC_INTRODUCER_EXTRACTORS")
        assert not hasattr(_gh_glab_hiding, "command_hides_gh_glab")

    def test_carve_out_posting_path_is_the_purity_proof(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``carve_out_applies`` (non-commit branch) routes through the purity
        # proof: patching the proof to a constant flips the verdict, proving it
        # is the decision path and not a separate detector.
        cfg = _config(tmp_path, [_PRIV_NS])
        cwd = _repo_with_remote(tmp_path / "cwd", _PRIV_REMOTE)
        monkeypatch.setattr(publish_surface, "command_is_pure_private_gh_glab_post", lambda *a, **k: False)
        assert (
            publish_surface.carve_out_applies(
                "Bash", f"gh issue create --repo {_PRIV_SLUG} --body {_TERM}", _TERM, cwd, config_path=cfg
            )
            is False
        )
        monkeypatch.setattr(publish_surface, "command_is_pure_private_gh_glab_post", lambda *a, **k: True)
        assert (
            publish_surface.carve_out_applies(
                "Bash", f"gh issue create --repo {_PUBLIC_SLUG} --body {_TERM}", _TERM, cwd, config_path=cfg
            )
            is True
        )


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

    def test_visibility_unknown_returns_commit_slug_for_chained_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The add-to-allowlist diagnostic must see the chained worktree-commit
        # idiom ``cd <wt> && git add -A && git commit …`` and report the
        # unresolvable target slug, exactly as it does for a plain commit. With
        # only first-action recognition the chained commit is invisible to the
        # hint and the operator gets no offline-allowlist guidance (#2215).
        cfg = _config(tmp_path, [])  # not allowlisted
        repo = _repo_with_remote(tmp_path / "r", _PRIV_REMOTE)
        monkeypatch.setenv("PATH", _git_only_bin(tmp_path / "gitonly"))
        monkeypatch.setattr(_repo_visibility, "_PROBE_PATH_EXTRA", ())
        slug = publish_surface.visibility_unknown_for_block(
            f'cd {repo} && git add -A && git commit -m "x"', repo, config_path=cfg
        )
        assert slug == publish_surface.slug_for_cwd(repo)

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
