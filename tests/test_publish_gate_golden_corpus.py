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
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from teatree.hooks import _repo_visibility, banned_terms_scanner, publish_destination, publish_surface

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


def _make_gh_shim(bin_dir: Path, visibility: str) -> None:
    """Write a ``gh`` shim answering ``repo view ... visibility`` with ``visibility``.

    The shim stands in for the live forge subprocess (the one external the
    visibility probe must call). Any slug routes to the same verdict, which is
    enough to prove the probe-resolved-from-the-command path classifies a
    target the offline allowlist does not know about.
    """
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


def _require_git() -> str:
    """Return the absolute path to the real ``git`` (for symlinking onto a test PATH)."""
    real_git = shutil.which("git")
    assert real_git is not None
    return real_git


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

    Mirrors ``hook_router._run_banned_terms_pretool``: a secret on ANY surface
    (body, title, short ``-t`` flag, ``gh api`` field, ``git -C`` commit
    subject) always blocks, checked BEFORE the payload-None early-return; the
    destination gate skips a PROVABLY-private target (offline allowlist or live
    probe, resolved from the COMMAND target -- so the leak gate enforces on
    PUBLIC targets only); otherwise the payload is scanned and a banned-term
    match (or a fail-closed sentinel) blocks unless the private-repo commit
    carve-out downgrades it.
    """
    tool_input = {"command": command}
    if publish_surface.contains_secret(banned_terms_scanner.secret_scan_text("Bash", tool_input)):
        return "block"
    payload = banned_terms_scanner.extract_publish_payload("Bash", tool_input)
    if payload is None:
        return "allow"
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

    def test_git_c_commit_to_private_repo_with_domain_word_allowed(self, config: Path, tmp_path: Path) -> None:
        # Vector 5 over-block guard: token-aware ``git -C`` detection must NOT
        # over-block a legitimate private-repo commit carrying a domain word.
        repo = _repo_with_remote(tmp_path / "priv", "git@gitlab.com:acme-internal/svc.git")
        cmd = f'git -C {repo} commit -m "feat: acmecorp purpose"'
        assert _verdict(cmd, None, config) == "allow"

    def test_internal_post_with_git_push_chain_allowed(self, config: Path) -> None:
        # Over-block guard: a chained inert ``git push`` after an internal post
        # must stay skip-safe (the V1 inversion fails closed only on a forge
        # transport / unrecognised executable, not on a plain inert segment).
        cmd = 'gh pr create -R internalcorp/svc --body "acmecorp internal" && git push origin main'
        assert _verdict(cmd, None, config) == "allow"


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

    def test_interpreter_transport_public_post_behind_internal_segment(self, config: Path) -> None:
        # Vector 1: a leading internal post used to make the WHOLE command skip
        # scanning; the chained ``sh -c "gh ... public"`` interpreter segment is
        # an opaque forge transport, so the gate now fails closed.
        cmd = 'glab mr create -R acme-internal/x --title ok && sh -c "gh pr create -R souliane/teatree --body acmecorp"'
        assert _verdict(cmd, None, config) == "block"

    @pytest.mark.parametrize(
        "wrapper",
        ["make publish", "npm run release", "python deploy.py", "./release.sh"],
        ids=["make", "npm", "python", "shell-script"],
    )
    def test_unrecognised_executable_chain_does_not_skip_scan(self, config: Path, wrapper: str) -> None:
        # Vector 1 extension: a chained UNRECOGNISED build/script runner carries
        # no literal forge token in its own argv yet can shell out to a public
        # post. A leading provably-internal segment must NOT make the gate skip
        # the whole command's leak scan -- the skip decision is the load-bearing
        # safety boundary the scanner-visible ``_verdict`` cannot reach (it never
        # sees the wrapper's opaque recipe), so the gate is asserted directly.
        cmd = f"glab mr create -R acme-internal/x --title ok && {wrapper}"
        assert publish_destination.gate_skips_destination(cmd, None, config_path=config) is False

    def test_raw_rest_with_interspersed_persistent_flag(self, config: Path) -> None:
        # Vector 2: an interspersed ``--hostname`` broke the contiguous ``gh api ``
        # substring, so the body was never extracted. Token-aware ``api``-position
        # detection now extracts and scans it.
        cmd = "gh --hostname github.com api repos/souliane/teatree/issues -f body=acmecorp"
        assert _verdict(cmd, None, config) == "block"

    def test_unreadable_body_file_to_public_repo_fails_closed(self, config: Path, tmp_path: Path) -> None:
        # Vector 3: a ``--body-file`` the gate cannot read injects the fail-closed
        # sentinel; the banned-terms scanner now blocks on it (the two sibling
        # scanners already did).
        missing = tmp_path / "absent" / "body.md"
        cmd = f"gh pr create -R souliane/teatree --title x --body-file {missing}"
        assert _verdict(cmd, None, config) == "block"

    def test_secret_in_short_title_flag_blocks(self, config: Path) -> None:
        # Vector 4: a secret in the ``-t`` short title flag (not the body) blocks.
        cmd = f'gh pr create -R souliane/teatree -t "release {_FAKE_SECRET}"'
        assert _verdict(cmd, None, config) == "block"

    def test_secret_in_api_title_field_blocks(self, config: Path) -> None:
        # Vector 4: a secret in a ``gh api -f title=`` field (not ``body=``) blocks.
        cmd = f"gh api repos/souliane/teatree/issues -f title={_FAKE_SECRET}"
        assert _verdict(cmd, None, config) == "block"

    def test_secret_in_internal_short_title_flag_blocks(self, config: Path) -> None:
        # Vector 4: secrets block on EVERY destination, including an internal post
        # the destination gate would otherwise SKIP.
        cmd = f'gh pr create -R acme-internal/x -t "release {_FAKE_SECRET}"'
        assert _verdict(cmd, None, config) == "block"

    def test_git_c_commit_to_public_repo_blocks(self, config: Path, tmp_path: Path) -> None:
        # Vector 5: ``git -C <public> commit -m`` used to slip the contiguous
        # ``git commit -m`` substring (the ``-C <dir>`` flag broke it). Token-aware
        # commit detection now reaches the gate and the public target blocks --
        # symmetric with ``cd <public> && git commit``.
        repo = _repo_with_remote(tmp_path / "pub", "git@github.com:souliane/teatree.git")
        cmd = f'git -C {repo} commit -m "acmecorp"'
        assert _verdict(cmd, None, config) == "block"

    def test_git_c_commit_secret_to_private_repo_blocks(self, config: Path, tmp_path: Path) -> None:
        # Vector 5 + secret-always: a secret in a ``git -C <private> commit``
        # subject blocks even though the private repo's domain words are exempt.
        repo = _repo_with_remote(tmp_path / "priv", "git@gitlab.com:acme-internal/svc.git")
        cmd = f'git -C {repo} commit -m "release {_FAKE_SECRET}"'
        assert _verdict(cmd, None, config) == "block"


class TestPrivateReposAllowlistReuse:
    """Fix 3: a ``private_repos``-only config (no ``internal_publish_namespaces``) skips internal posts."""

    def test_private_repos_only_skips_internal_post(self, private_repos_only_config: Path) -> None:
        cmd = 'glab mr create -R acme-internal/x --title "feat: acmecorp" --description "acmecorp acmewidget"'
        assert _verdict(cmd, None, private_repos_only_config) == "allow"

    def test_private_repos_only_still_blocks_public(self, private_repos_only_config: Path) -> None:
        cmd = 'gh pr create -R souliane/teatree --title "x" --body "acmecorp"'
        assert _verdict(cmd, None, private_repos_only_config) == "block"


# Entry-point detection spellings the gate must scan-or-deny. Each row is a
# command shape that carries a banned term or secret toward a PUBLIC surface
# through a DIFFERENT spelling of the publish/commit/api detection. The
# anti-whack-a-mole meta-test pins all of them: a future un-enumerated spelling
# that slips a detector trips this list (the spelling would ALLOW). Synthetic
# terms only.
# A leading provably-internal post; the chained public-leaking interpreter
# segment is what each interpreter row varies.
_INTERNAL_LEAD = "glab mr create -R acme-internal/x && "
_PUBLIC_POST = "gh pr create -R souliane/teatree --body acmecorp"

_LEAK_SPELLINGS: list[tuple[str, str]] = [
    ("body long flag", 'gh pr create -R souliane/teatree --body "acmecorp"'),
    ("body short flag -b", 'gh pr create -R souliane/teatree -b "acmecorp"'),
    ("title long flag secret", f'gh pr create -R souliane/teatree --title "release {_FAKE_SECRET}"'),
    ("title short flag -t secret", f'gh pr create -R souliane/teatree -t "release {_FAKE_SECRET}"'),
    ("glab note long body", 'glab mr note 7 -R souliane/teatree --message "acmecorp"'),
    ("gh api contiguous body", "gh api repos/souliane/teatree/issues -f body=acmecorp"),
    ("gh api interspersed --hostname", "gh --hostname github.com api repos/souliane/teatree/issues -f body=acmecorp"),
    ("gh api -X interspersed", "gh -X POST api repos/souliane/teatree/issues -f body=acmecorp"),
    ("gh api title field secret", f"gh api repos/souliane/teatree/issues -f title={_FAKE_SECRET}"),
    ("glab api interspersed", "glab --hostname gitlab.com api projects/souliane%2Fteatree/issues -f body=acmecorp"),
    ("interpreter sh -c forge", f'{_INTERNAL_LEAD}sh -c "{_PUBLIC_POST}"'),
    ("interpreter bash -c forge", f'{_INTERNAL_LEAD}bash -c "{_PUBLIC_POST}"'),
    ("interpreter eval forge", f'{_INTERNAL_LEAD}eval "{_PUBLIC_POST}"'),
    ("ssh wrapper forge", f"{_INTERNAL_LEAD}ssh host {_PUBLIC_POST}"),
    ("xargs wrapper forge", f"{_INTERNAL_LEAD}echo x | xargs {_PUBLIC_POST}"),
]


class TestEntryPointSpellingsMetaTest:
    """Pin every publish/commit/api detection spelling against the anti-whack-a-mole doctrine.

    Each enumerated leak spelling toward a PUBLIC destination carrying a banned
    term or secret MUST block. A new un-enumerated spelling that slips a
    detector would ALLOW and trip this test -- the meta-test is the receipt that
    the entry points stay closed, not a per-instance patch.
    """

    @pytest.mark.parametrize(("label", "command"), _LEAK_SPELLINGS, ids=[row[0] for row in _LEAK_SPELLINGS])
    def test_public_leak_spelling_blocks(self, label: str, command: str, config: Path) -> None:
        assert _verdict(command, None, config) == "block", f"spelling not scanned/denied: {label}"

    def test_git_commit_global_flag_spellings_reach_the_gate(self, config: Path, tmp_path: Path) -> None:
        # The ``git [global-flag] commit`` spellings (``-C``, ``--git-dir=``,
        # the ``--message`` long form) must all reach the gate against a public
        # repo; a contiguous-substring detector missed each interspersed flag.
        repo = _repo_with_remote(tmp_path / "pub", "git@github.com:souliane/teatree.git")
        gitdir = repo / ".git"
        spellings = [
            f'git -C {repo} commit -m "acmecorp"',
            f'git --git-dir={gitdir} --work-tree={repo} commit -m "acmecorp"',
            f'git -C {repo} commit --message "acmecorp"',
        ]
        for cmd in spellings:
            assert _verdict(cmd, None, config) == "block", f"commit spelling slipped the gate: {cmd}"


class TestProbeResolvedTargetVisibility:
    """The gate classifies the COMMAND's target, not the harness cwd.

    A post FROM a public clone (the harness cwd) TO a target the offline
    allowlist does not name must resolve THAT target's visibility from the
    command -- the ``--repo``/``-R`` flag or the destination's own git remote --
    and consult the live ``gh``/``glab`` probe, the same fallback the
    private-repo carve-out already applies. The fixture config carries NO
    allowlist entry for the probe-resolved repos, so only the live probe can
    prove them private; the harness cwd is the genuinely-public ``souliane/
    teatree`` clone, exactly the over-block this fix removes.

    The probe's one external -- the ``gh`` subprocess -- is stubbed by a PATH
    shim; the day-cache is isolated via ``T3_DATA_DIR`` so a stale verdict from
    the real cache never leaks in.
    """

    @pytest.fixture
    def empty_allowlist_config(self, tmp_path: Path) -> Path:
        # No private_repos / internal_publish_namespaces entry: a target is
        # provably private ONLY through the live probe, never the allowlist.
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teatree]\nbanned_terms = ["acmecorp", "acmewidget"]\n', encoding="utf-8")
        return cfg

    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "vis-cache"))

    def _public_cwd(self, tmp_path: Path) -> Path:
        return _repo_with_remote(tmp_path / "public-clone", "git@github.com:souliane/teatree.git")

    def test_flag_target_private_via_probe_from_public_cwd_allows(
        self, empty_allowlist_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # must-ALLOW (RED on current main): a banned/domain term posted to a
        # PROVABLY-private ``--repo`` target FROM the public teatree clone. On
        # current main the destination skip ignores the probe and classifies
        # the target PUBLIC -> the post is over-blocked.
        bin_dir = tmp_path / "bin"
        _make_gh_shim(bin_dir, "PRIVATE")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        cwd = self._public_cwd(tmp_path)
        cmd = 'gh issue comment 5 --repo privowner/private-svc --body "acmecorp domain note"'
        assert _verdict(cmd, cwd, empty_allowlist_config) == "allow"

    def test_worktree_cwd_target_private_via_probe_allows(
        self, empty_allowlist_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # must-ALLOW: a flagless post whose target is the CWD's own private-repo
        # worktree (its git remote), with no ``--repo`` flag. The probe resolves
        # the cwd-remote slug to private, so the domain term is allowed.
        bin_dir = tmp_path / "bin"
        _make_gh_shim(bin_dir, "PRIVATE")
        (bin_dir / "git").symlink_to(_require_git())
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        worktree = _repo_with_remote(tmp_path / "priv-wt", "git@github.com:privowner/private-svc.git")
        cmd = 'gh pr create --title "feat: x" --body "acmecorp domain note"'
        assert _verdict(cmd, worktree, empty_allowlist_config) == "allow"

    def test_flag_target_public_via_probe_blocks(
        self, empty_allowlist_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # must-DENY: the SAME term posted to a probe-PUBLIC ``--repo`` target
        # stays blocked. The fix narrows the over-block to the provably-private
        # case; the public-leak path is unchanged.
        bin_dir = tmp_path / "bin"
        _make_gh_shim(bin_dir, "PUBLIC")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        cwd = self._public_cwd(tmp_path)
        cmd = 'gh issue comment 5 --repo someowner/open-svc --body "acmecorp domain note"'
        assert _verdict(cmd, cwd, empty_allowlist_config) == "block"

    def test_unresolvable_target_fails_closed_blocks(
        self, empty_allowlist_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # must-DENY (fail-closed): when the probe cannot prove the target private
        # (probe returns the UNKNOWN ``None`` -- tool absent in-hook or auth
        # differs) and the allowlist does not name it, the target is PUBLIC and
        # the term blocks. Detection failure never opens. The probe is stubbed to
        # the deterministic UNKNOWN verdict rather than relying on a flaky network
        # call to a non-existent repo.
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        monkeypatch.delenv("GH_REPO", raising=False)
        cwd = self._public_cwd(tmp_path)
        cmd = 'gh issue comment 5 --repo unknown/mystery --body "acmecorp domain note"'
        assert _verdict(cmd, cwd, empty_allowlist_config) == "block"

    def test_clean_post_to_private_probe_target_never_blocks(
        self, empty_allowlist_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # never-lockout: a clean post (no banned term, no secret) is allowed
        # regardless of the resolved target's visibility.
        bin_dir = tmp_path / "bin"
        _make_gh_shim(bin_dir, "PRIVATE")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        cwd = self._public_cwd(tmp_path)
        cmd = 'gh issue comment 5 --repo privowner/private-svc --body "a clean status update"'
        assert _verdict(cmd, cwd, empty_allowlist_config) == "allow"

    def test_clean_post_to_public_probe_target_never_blocks(
        self, empty_allowlist_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # never-lockout: a clean post to a public target is also allowed.
        bin_dir = tmp_path / "bin"
        _make_gh_shim(bin_dir, "PUBLIC")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        cwd = self._public_cwd(tmp_path)
        cmd2 = 'gh issue comment 5 --repo someowner/open-svc --body "a clean status update"'
        assert _verdict(cmd2, cwd, empty_allowlist_config) == "allow"


class TestLeakGateEnforcesOnPublicTargetsOnly:
    """The leak gate enforces on PUBLIC targets ONLY (the user's explicit rule).

    A customer/banned term is blocked when -- and only when -- the COMMAND's
    resolved target is PUBLIC (or its visibility cannot be determined, which
    fails closed to strict). On ANY private target -- the user's OWN private
    overlay repo AND a customer's own (colleague) private repo -- the gate does
    NOT block: the destination skip resolves the real target from the command
    and skips the scan. SYNTHETIC namespaces only; the private ones are proven
    private via the allowlist and via the live ``gh`` probe shim.
    """

    @pytest.fixture
    def allowlist_config(self, tmp_path: Path) -> Path:
        # Both a private OWN overlay namespace and a private COLLEAGUE namespace
        # are declared private offline; ``souliane/teatree`` stays public.
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text(
            "[teatree]\n"
            'private_repos = ["ownoverlay-org", "customer-org"]\n'
            'banned_terms = ["customercorp", "customerwidget"]\n',
            encoding="utf-8",
        )
        return cfg

    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "vis-cache"))

    def _public_cwd(self, tmp_path: Path) -> Path:
        return _repo_with_remote(tmp_path / "public-clone", "git@github.com:souliane/teatree.git")

    def test_customer_term_to_public_repo_blocks(self, allowlist_config: Path, tmp_path: Path) -> None:
        # must-DENY: a customer term toward the genuinely-public repo.
        cwd = self._public_cwd(tmp_path)
        cmd = 'gh issue comment 5 --repo souliane/teatree --body "customercorp leak"'
        assert _verdict(cmd, cwd, allowlist_config) == "block"

    def test_customer_term_to_own_private_overlay_repo_allows(self, allowlist_config: Path, tmp_path: Path) -> None:
        # must-ALLOW: a customer term toward the user's OWN private overlay repo.
        cwd = self._public_cwd(tmp_path)
        cmd = 'gh pr create --repo ownoverlay-org/t3-tool --title "feat: x" --body "customercorp here"'
        assert _verdict(cmd, cwd, allowlist_config) == "allow"

    def test_customer_term_to_colleague_private_repo_allows(self, allowlist_config: Path, tmp_path: Path) -> None:
        # must-ALLOW: a customer term toward the customer's own (colleague)
        # private repo -- the user's explicit rule overrules per-term blocking.
        cwd = self._public_cwd(tmp_path)
        cmd = 'gh issue comment 5 --repo customer-org/their-svc --body "customercorp customerwidget note"'
        assert _verdict(cmd, cwd, allowlist_config) == "allow"

    def test_customer_term_to_colleague_repo_via_url_positional_allows(
        self, allowlist_config: Path, tmp_path: Path
    ) -> None:
        # must-ALLOW: target resolved from a forge URL positional (no --repo
        # flag) to the colleague private repo.
        cwd = self._public_cwd(tmp_path)
        cmd = 'gh issue comment https://github.com/customer-org/their-svc/issues/5 --body "customercorp note"'
        assert _verdict(cmd, cwd, allowlist_config) == "allow"

    def test_commit_in_private_worktree_from_public_cwd_allows(self, allowlist_config: Path, tmp_path: Path) -> None:
        # must-ALLOW: a git -C commit whose worktree (resolved FROM THE COMMAND)
        # is the private colleague repo, even though the harness cwd is the
        # public clone -- the cwd->target resolution part of the fix.
        worktree = _repo_with_remote(tmp_path / "wt", "git@gitlab.com:customer-org/their-svc.git")
        cwd = self._public_cwd(tmp_path)
        cmd = f'git -C {worktree} commit -m "customercorp feature"'
        assert _verdict(cmd, cwd, allowlist_config) == "allow"

    def test_pr_create_in_private_worktree_cwd_allows(self, allowlist_config: Path, tmp_path: Path) -> None:
        # must-ALLOW: a flagless gh pr create whose CWD is the private-repo
        # worktree (its git remote resolves the private target).
        worktree = _repo_with_remote(tmp_path / "wt", "git@github.com:ownoverlay-org/t3-tool.git")
        cmd = 'gh pr create --title "feat: x" --body "customercorp here"'
        assert _verdict(cmd, worktree, allowlist_config) == "allow"

    def test_customer_term_to_unresolvable_target_fails_closed(
        self, allowlist_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # fail-closed: an undeclared target whose visibility cannot be determined
        # (probe returns the UNKNOWN None) is treated as PUBLIC/strict and blocks.
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        cwd = self._public_cwd(tmp_path)
        cmd = 'gh issue comment 5 --repo unknown/mystery --body "customercorp note"'
        assert _verdict(cmd, cwd, allowlist_config) == "block"

    def test_colleague_private_proven_only_by_probe_allows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # must-ALLOW: a colleague repo NOT in the offline allowlist, proven
        # private ONLY by the live probe, still skips -- the visibility is
        # resolved from the command target via the probe.
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teatree]\nbanned_terms = ["customercorp"]\n', encoding="utf-8")
        bin_dir = tmp_path / "bin"
        _make_gh_shim(bin_dir, "PRIVATE")
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        cwd = self._public_cwd(tmp_path)
        cmd = 'gh issue comment 5 --repo probeonly-org/svc --body "customercorp note"'
        assert _verdict(cmd, cwd, cfg) == "allow"

    def test_clean_post_to_public_never_blocks(self, allowlist_config: Path, tmp_path: Path) -> None:
        # never-lockout: a clean post (no banned term) to a public target.
        cwd = self._public_cwd(tmp_path)
        cmd = 'gh issue comment 5 --repo souliane/teatree --body "a clean status update"'
        assert _verdict(cmd, cwd, allowlist_config) == "allow"
