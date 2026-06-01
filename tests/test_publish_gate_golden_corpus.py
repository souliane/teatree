"""Golden must-ALLOW / must-DENY corpus for the destination-aware publish gate.

The load-bearing safety artifact for the publish-surface privacy gate. Each
row pins the gate's verdict on a real command shape against a fixture config
that mirrors the user's actual private-repo allowlist -- one dimension proves
the gate does NOT leak (every public/chained/substituted post is scanned and
blocks on a banned term or secret), the other proves it does NOT lock the user
out (legitimate internal/private and local-only work is allowed). The corpus
is the regression guard for the five fixes:

1. ALL-SEGMENTS skip (a chained public post behind an internal segment scans);
2. fail-closed on substitution / transport / raw-REST api;
3. destination classification reuses the existing ``private_repos`` allowlist;
4. file-based bodies (``--description-file``) are honoured;
5. the commit gate resolves the real repo (``cd`` / walk-up) and fails OPEN on
    a truly-unresolvable LOCAL commit while a resolvable-PUBLIC commit blocks.

Synthetic namespaces / banned terms only (``acme-internal``, ``internalcorp``,
``acmecorp``, ``acmewidget``, and the genuinely-public ``souliane/teatree``);
the real allowlist lives in the user's private config, never in the source or
tests, and the fixture config is injected so the test NEVER reads the real
``~/.teatree.toml``.
"""

import os
import subprocess
from pathlib import Path

import pytest

from teatree.hooks import banned_terms_scanner, publish_destination, publish_surface

# A high-confidence fake secret (never a real credential): a GitHub PAT shape.
_FAKE_SECRET = "ghp_" + "A" * 40


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


@pytest.fixture
def config(tmp_path: Path) -> Path:
    # Both allowlists name the (synthetic) private namespaces: ``internal_
    # publish_namespaces`` is what the first-segment-only skip consulted (so
    # the chained / substitution leaks fail RED on the pre-fix code), and
    # ``private_repos`` exercises the carve-out / commit path.
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        "[teatree]\n"
        'private_repos = ["acme-internal", "internalcorp"]\n'
        'internal_publish_namespaces = ["acme-internal", "internalcorp"]\n'
        'banned_terms = ["acmecorp", "acmewidget"]\n',
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def private_repos_only_config(tmp_path: Path) -> Path:
    # Fix 3: the user's CURRENT config has only ``private_repos`` (no
    # ``internal_publish_namespaces`` key). The destination skip must still
    # fire for those namespaces by reusing the existing allowlist.
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        '[teatree]\nprivate_repos = ["acme-internal", "internalcorp"]\nbanned_terms = ["acmecorp", "acmewidget"]\n',
        encoding="utf-8",
    )
    return cfg


def _verdict(command: str, cwd: Path | None, config_path: Path) -> str:
    """Return ``"allow"`` or ``"block"`` for a Bash ``command`` under ``config``.

    Mirrors ``hook_router._run_banned_terms_pretool``: a secret always blocks;
    the destination gate skips a provably-internal target; otherwise the
    payload is scanned and a banned-term match blocks unless the private-repo
    carve-out downgrades it.
    """
    tool_input = {"command": command}
    payload = banned_terms_scanner.extract_publish_payload("Bash", tool_input)
    if payload is None:
        return "allow"
    if publish_surface.contains_secret(payload):
        return "block"
    skipped = banned_terms_scanner.has_override("Bash", tool_input) or publish_destination.gate_skips_destination(
        command, cwd, config_path=config_path
    )
    if skipped or banned_terms_scanner.scan_text(payload, config_path=config_path) is None:
        return "allow"
    if publish_surface.carve_out_applies("Bash", command, payload, cwd, config_path=config_path):
        return "allow"
    return "block"


class TestMustAllow:
    """Legitimate private/internal/local work the gate must NOT block."""

    def test_internal_glab_mr_inline_body(self, config: Path) -> None:
        cmd = (
            "glab mr create -R acme-internal/team/microservice-x "
            '--title "feat: acmecorp purpose" --description "acmecorp acmewidget purpose"'
        )
        assert _verdict(cmd, None, config) == "allow"

    def test_internal_glab_mr_description_file(self, config: Path, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text("## What\nacmecorp acmewidget purpose\n", encoding="utf-8")
        cmd = f"glab mr create -R acme-internal/x --title 'feat: acmecorp' --description-file {body}"
        assert _verdict(cmd, None, config) == "allow"

    def test_internal_gh_pr(self, config: Path) -> None:
        cmd = 'gh pr create -R internalcorp/private-svc --title "feat: acmecorp" --body "acmecorp internal"'
        assert _verdict(cmd, None, config) == "allow"

    def test_commit_resolves_via_cd_to_private_repo(self, config: Path, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@gitlab.com:acme-internal/microservice-x.git")
        cmd = f'cd {repo} && git commit -m "feat: acmecorp purpose"'
        assert _verdict(cmd, None, config) == "allow"

    def test_bare_commit_in_non_git_workspace_root_fails_open(self, config: Path, tmp_path: Path) -> None:
        # Payload cwd is the workspace root (NOT a git repo): a bare commit
        # there is purely local and cannot leak -> fail-open.
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        cmd = 'git commit -m "feat: acmecorp purpose"'
        assert _verdict(cmd, workspace_root, config) == "allow"


class TestMustDeny:
    """Real leak boundaries the gate must scan/block."""

    def test_public_repo_post(self, config: Path) -> None:
        cmd = 'gh pr create -R souliane/teatree --title "x" --body "acmecorp"'
        assert _verdict(cmd, None, config) == "block"

    def test_chained_internal_then_public_post(self, config: Path) -> None:
        cmd = 'glab mr create -R acme-internal/x && gh pr create -R souliane/teatree --body "acmecorp"'
        assert _verdict(cmd, None, config) == "block"

    def test_substitution_public_post_inside_internal_body(self, config: Path) -> None:
        cmd = 'glab mr create -R acme-internal/x --description "$(gh pr create -R souliane/teatree --body acmecorp)"'
        assert _verdict(cmd, None, config) == "block"

    def test_raw_rest_api_to_public(self, config: Path) -> None:
        cmd = "gh api repos/souliane/teatree/issues -f body=acmecorp"
        assert _verdict(cmd, None, config) == "block"

    def test_commit_resolving_to_public_repo_blocks(self, config: Path, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "pub", "git@github.com:souliane/teatree.git")
        cmd = f'cd {repo} && git commit -m "acmecorp"'
        assert _verdict(cmd, None, config) == "block"

    def test_secret_on_internal_post_still_blocks(self, config: Path) -> None:
        # A secret is blocked on EVERY surface, including an internal post
        # whose DESTINATION the gate would otherwise SKIP. The secret block
        # runs before any skip short-circuit.
        cmd = f'gh pr create -R acme-internal/x --title "feat: acmecorp" --body "token {_FAKE_SECRET}"'
        assert _verdict(cmd, None, config) == "block"

    def test_secret_on_public_post_blocks(self, config: Path) -> None:
        cmd = f'gh pr create -R souliane/teatree --title "x" --body "token {_FAKE_SECRET}"'
        assert _verdict(cmd, None, config) == "block"


class TestPrivateReposAllowlistReuse:
    """Fix 3: a ``private_repos``-only config (no ``internal_publish_namespaces``) skips internal posts."""

    def test_private_repos_only_skips_internal_post(self, private_repos_only_config: Path) -> None:
        cmd = 'glab mr create -R acme-internal/x --title "feat: acmecorp" --description "acmecorp acmewidget"'
        assert _verdict(cmd, None, private_repos_only_config) == "allow"

    def test_private_repos_only_still_blocks_public(self, private_repos_only_config: Path) -> None:
        cmd = 'gh pr create -R souliane/teatree --title "x" --body "acmecorp"'
        assert _verdict(cmd, None, private_repos_only_config) == "block"
