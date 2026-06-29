"""Tests for the destination-aware gate skip (`teatree.hooks.publish_destination`).

``resolve_publish_destination`` extracts the target repo/namespace of a
publish command; ``is_public_destination`` classifies it FAIL-CLOSED
(PUBLIC unless its namespace provably matches the config-driven
``[teatree] internal_publish_namespaces`` / ``T3_INTERNAL_PUBLISH_NAMESPACES``
allowlist); ``gate_skips_destination`` is the composed predicate the
banned-terms (#1415) and bare-reference (#1530) gates call to scan only
PUBLIC targets.

Synthetic namespaces only (``internalcorp``, ``acme-internal``, the
genuinely-public ``souliane/teatree``); the allowlist lives in the user's
private config, never in the source or tests.
"""

import os
import subprocess
from pathlib import Path

import pytest

from teatree.hooks import _repo_visibility, publish_destination


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            # Deterministic identity so ``git commit`` / ``git worktree add``
            # succeed under an identity-less git: the CI container has no global
            # user.name/email and auto-detection is disabled, so no inherited
            # identity can be assumed.
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


def _repo_with_remote(path: Path, remote_url: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "remote", "add", "origin", remote_url)
    return path


def _config(tmp_path: Path, namespaces: list[str]) -> Path:
    cfg = tmp_path / ".teatree.toml"
    entries = ", ".join(f'"{n}"' for n in namespaces)
    cfg.write_text(f"[teatree]\ninternal_publish_namespaces = [{entries}]\n", encoding="utf-8")
    return cfg


class TestResolvePublishDestination:
    """``resolve_publish_destination`` extracts the target repo/namespace.

    Covers the explicit ``--repo``/``-R`` flag (last-wins), the raw-REST
    ``gh api repos/...`` / ``glab api projects/...`` URL paths, the
    ``GH_REPO`` env default, the flagless create/comment current-repo
    fallback, and the unresolvable cases that must return ``None`` (so the
    caller treats the destination as PUBLIC and scans).
    """

    def test_gh_repo_flag(self) -> None:
        dest = publish_destination.resolve_publish_destination("gh pr create -R acme-internal/app --title x")
        assert dest is not None
        assert dest.slug == "acme-internal/app"
        assert dest.via == "flag"

    def test_glab_repo_flag(self) -> None:
        dest = publish_destination.resolve_publish_destination("glab mr create -R internalcorp/private-svc --title x")
        assert dest is not None
        assert dest.slug == "internalcorp/private-svc"

    def test_repeated_repo_flag_last_wins(self) -> None:
        dest = publish_destination.resolve_publish_destination(
            "gh pr create --repo internalcorp/private-svc --repo souliane/teatree --title x"
        )
        assert dest is not None
        assert dest.slug == "souliane/teatree"

    def test_gh_api_repos_path(self) -> None:
        dest = publish_destination.resolve_publish_destination(
            "gh api repos/acme-internal/app/issues -f body=x --method POST"
        )
        assert dest is not None
        assert dest.slug == "acme-internal/app"
        assert dest.via == "api"

    def test_glab_api_projects_url_encoded_path(self) -> None:
        dest = publish_destination.resolve_publish_destination(
            "glab api projects/internalcorp%2Fprivate-svc/merge_requests/1/notes -f body=x"
        )
        assert dest is not None
        assert dest.slug == "internalcorp/private-svc"

    def test_glab_api_projects_nested_namespace(self) -> None:
        dest = publish_destination.resolve_publish_destination(
            "glab api projects/internalcorp%2Fteam%2Fprivate-svc/issues"
        )
        assert dest is not None
        assert dest.slug == "internalcorp/team/private-svc"

    def test_gh_api_non_repos_path_is_none(self) -> None:
        assert publish_destination.resolve_publish_destination("gh api user/repos") is None

    def test_github_issue_url_positional_resolves_target(self) -> None:
        dest = publish_destination.resolve_publish_destination(
            "gh issue comment https://github.com/owner/repo/issues/5 --body x"
        )
        assert dest is not None
        assert dest.slug == "owner/repo"
        assert dest.via == "url"

    def test_gitlab_mr_url_positional_with_dash_infix_resolves_nested_namespace(self) -> None:
        dest = publish_destination.resolve_publish_destination(
            "glab mr note https://gitlab.com/group/sub/repo/-/merge_requests/3 --message x"
        )
        assert dest is not None
        assert dest.slug == "group/sub/repo"

    def test_bare_repo_url_positional_strips_git_suffix(self) -> None:
        dest = publish_destination.resolve_publish_destination("gh pr create https://github.com/owner/repo.git")
        assert dest is not None
        assert dest.slug == "owner/repo"

    def test_url_positional_wins_over_cwd_remote(self, tmp_path: Path) -> None:
        # A forge URL positional is more specific than the cwd remote, so it
        # resolves the target even when cwd is a different repo.
        repo = _repo_with_remote(tmp_path / "r", "git@github.com:acme-internal/app.git")
        dest = publish_destination.resolve_publish_destination(
            "gh issue comment https://github.com/owner/repo/issues/5 --body x", repo
        )
        assert dest is not None
        assert dest.slug == "owner/repo"

    def test_gh_pr_create_no_flag_resolves_current_repo(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "r", "git@github.com:acme-internal/app.git")
        dest = publish_destination.resolve_publish_destination("gh pr create --title x", repo)
        assert dest is not None
        assert dest.slug == "github.com/acme-internal/app"
        assert dest.via == "cwd"

    def test_gh_env_repo_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH_REPO", "acme-internal/app")
        dest = publish_destination.resolve_publish_destination("gh pr create --title x")
        assert dest is not None
        assert dest.slug == "acme-internal/app"
        assert dest.via == "env"

    def test_explicit_flag_wins_over_gh_env_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH_REPO", "acme-internal/app")
        dest = publish_destination.resolve_publish_destination("gh pr create --repo souliane/teatree --title x")
        assert dest is not None
        assert dest.slug == "souliane/teatree"

    def test_non_gh_glab_command_is_none(self) -> None:
        assert publish_destination.resolve_publish_destination("curl -d body=x https://example.com") is None

    def test_flagless_create_without_cwd_is_none(self) -> None:
        assert publish_destination.resolve_publish_destination("gh pr create --title x", None) is None

    def test_command_after_separator_does_not_resolve(self) -> None:
        assert publish_destination.resolve_publish_destination("echo hi && gh pr create -R acme-internal/app") is None

    def test_non_posting_gh_verb_is_none(self) -> None:
        # ``glab mr list`` is neither a flag/api/create target → None.
        assert publish_destination.resolve_publish_destination("glab mr list") is None


class TestIsPublicDestination:
    """FAIL-CLOSED: PUBLIC unless the slug provably matches the internal allowlist."""

    def test_none_destination_is_public(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["internalcorp"])
        assert publish_destination.is_public_destination(None, config_path=cfg) is True

    def test_empty_allowlist_treats_internal_looking_slug_as_public(self, tmp_path: Path) -> None:
        # DEFAULT (key absent / empty) → behaviour unchanged, everything PUBLIC.
        cfg = _config(tmp_path, [])
        dest = publish_destination.Destination(slug="internalcorp/private-svc", via="flag")
        assert publish_destination.is_public_destination(dest, config_path=cfg) is True

    def test_missing_config_treats_everything_as_public(self, tmp_path: Path) -> None:
        dest = publish_destination.Destination(slug="internalcorp/private-svc", via="flag")
        assert publish_destination.is_public_destination(dest, config_path=tmp_path / "absent.toml") is True

    def test_allowlisted_namespace_is_internal(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["internalcorp"])
        dest = publish_destination.Destination(slug="internalcorp/private-svc", via="flag")
        assert publish_destination.is_public_destination(dest, config_path=cfg) is False

    def test_host_prefixed_slug_matches_namespace(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["gitlab.example/internalcorp"])
        dest = publish_destination.Destination(slug="gitlab.example/internalcorp/private-svc", via="cwd")
        assert publish_destination.is_public_destination(dest, config_path=cfg) is False

    def test_internal_namespace_match_is_host_qualification_symmetric(self, tmp_path: Path) -> None:
        # Unifying onto the host-stripping slug_namespace_matches (#1953) makes the
        # internal_publish_namespaces gate host-symmetric too: a bare entry now
        # matches a host-qualified slug, and a host-qualified entry matches a bare
        # slug. Both treat the destination as INTERNAL (leak-relaxing direction),
        # so pin them. The host segment never participates in the match.
        bare_dir = tmp_path / "bare"
        bare_dir.mkdir()
        bare_entry = _config(bare_dir, ["internalcorp"])
        host_qualified_slug = publish_destination.Destination(slug="github.com/internalcorp/svc", via="cwd")
        assert publish_destination.is_public_destination(host_qualified_slug, config_path=bare_entry) is False

        host_dir = tmp_path / "host"
        host_dir.mkdir()
        host_entry = _config(host_dir, ["gitlab.example/internalcorp"])
        bare_slug = publish_destination.Destination(slug="internalcorp/svc", via="flag")
        assert publish_destination.is_public_destination(bare_slug, config_path=host_entry) is False

    def test_genuinely_public_slug_stays_public(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["internalcorp"])
        dest = publish_destination.Destination(slug="souliane/teatree", via="flag")
        assert publish_destination.is_public_destination(dest, config_path=cfg) is True

    def test_segment_boundary_prevents_prefix_false_match(self, tmp_path: Path) -> None:
        # ``internalcorp`` must NOT match an unrelated ``internalcorp-public``.
        cfg = _config(tmp_path, ["internalcorp"])
        dest = publish_destination.Destination(slug="internalcorp-public/app", via="flag")
        assert publish_destination.is_public_destination(dest, config_path=cfg) is True

    def test_env_var_namespace_is_internal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, [])
        monkeypatch.setenv("T3_INTERNAL_PUBLISH_NAMESPACES", "internalcorp, acme-internal")
        dest = publish_destination.Destination(slug="acme-internal/app", via="flag")
        assert publish_destination.is_public_destination(dest, config_path=cfg) is False

    def test_empty_slug_is_public(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["internalcorp"])
        dest = publish_destination.Destination(slug="", via="flag")
        assert publish_destination.is_public_destination(dest, config_path=cfg) is True

    def test_exact_slug_match_is_internal(self, tmp_path: Path) -> None:
        # A whole-slug allowlist entry matches the slug exactly.
        cfg = _config(tmp_path, ["internalcorp/private-svc"])
        dest = publish_destination.Destination(slug="internalcorp/private-svc", via="flag")
        assert publish_destination.is_public_destination(dest, config_path=cfg) is False

    def test_malformed_config_treats_everything_as_public(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text("this is = not [valid toml", encoding="utf-8")
        dest = publish_destination.Destination(slug="internalcorp/private-svc", via="flag")
        assert publish_destination.is_public_destination(dest, config_path=cfg) is True


class TestGateSkipsDestination:
    """The composed predicate the gates call: SKIP only a provably-internal target."""

    def test_internal_flag_target_is_skipped(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["internalcorp"])
        assert (
            publish_destination.gate_skips_destination(
                "glab mr note 5 -R internalcorp/private-svc --message x", None, config_path=cfg
            )
            is True
        )

    def test_public_flag_target_is_not_skipped(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["internalcorp"])
        assert (
            publish_destination.gate_skips_destination(
                "gh pr create -R souliane/teatree --title x", None, config_path=cfg
            )
            is False
        )

    def test_unresolvable_destination_is_not_skipped(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["internalcorp"])
        assert (
            publish_destination.gate_skips_destination("curl -d x https://example.com", None, config_path=cfg) is False
        )

    @pytest.mark.parametrize(
        "wrapper",
        [
            "make publish",
            "npm run release",
            "python deploy.py",
            "./release.sh --notes x",
            "bundle exec rake post",
        ],
        ids=["make", "npm", "python", "shell-script", "rake"],
    )
    def test_unrecognised_executable_chain_fails_closed(self, tmp_path: Path, wrapper: str) -> None:
        # A chained UNRECOGNISED executable resolves to no destination and is not
        # a recognised inert leader -- it can shell out to a public post with no
        # forge token in its own argv. A leading provably-internal segment must
        # NOT let it skip the whole command's leak scan (fail-closed).
        cfg = _config(tmp_path, ["internalcorp"])
        cmd = f"gh pr create -R internalcorp/private-svc --body hi && {wrapper}"
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is False

    @pytest.mark.parametrize(
        "tail",
        ["git push origin main", "cd /tmp && echo done", "echo released", ": noop"],
        ids=["git-push", "cd-echo", "echo", "noop"],
    )
    def test_recognised_inert_local_chain_stays_skipped(self, tmp_path: Path, tail: str) -> None:
        # Over-block guard: a recognised navigation / git-transport / local-only
        # tail after an internal post is provably inert and must stay skip-safe,
        # so the fail-closed leader check does not needlessly re-block ordinary
        # local work chained off a legitimate internal publish.
        cfg = _config(tmp_path, ["internalcorp"])
        cmd = f'gh pr create -R internalcorp/private-svc --body "ok" && {tail}'
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is True

    @pytest.mark.parametrize(
        "read",
        [
            "gh api repos/souliane/teatree/issues",
            "glab api projects/souliane%2Fteatree/issues",
            "gh api repos/souliane/teatree/issues --method GET",
            "glab api projects/42/merge_requests -X GET",
        ],
        ids=["gh-get", "glab-get", "gh-method-get", "glab-x-get"],
    )
    def test_read_only_api_chain_stays_skipped(self, tmp_path: Path, read: str) -> None:
        # A read-only ``gh``/``glab api`` GET posts NO body, so it can never leak
        # content -- regardless of the repo its URL names. Chained after a
        # provably-internal post it must stay skip-safe, not over-block the whole
        # command on the bare ``api`` word (#1530 over-block).
        cfg = _config(tmp_path, ["internalcorp"])
        cmd = f'gh pr create -R internalcorp/private-svc --body "ok" && {read}'
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is True

    def test_api_write_chain_still_fails_closed(self, tmp_path: Path) -> None:
        # Leak guard: a chained ``api`` WRITE carries a body to an arbitrary
        # endpoint, so a leading internal post must NOT let it skip the leak scan.
        cfg = _config(tmp_path, ["internalcorp"])
        cmd = "gh pr create -R internalcorp/private-svc --body ok && gh api repos/souliane/teatree/issues -f body=x"
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is False

    @pytest.mark.parametrize(
        "write",
        [
            'glab api --method PUT "projects/internalcorp%2Fprivate-svc/merge_requests/7562" --input /tmp/body.json',
            "glab api projects/internalcorp%2Fprivate-svc/merge_requests/7562 -X PUT --input /tmp/body.json",
            "gh api repos/internalcorp/private-svc/issues -f body=hello",
            "glab api projects/internalcorp%2Fprivate-svc/issues/5/notes -f body=hello",
            "gh api /repos/internalcorp/private-svc/issues -f body=hello",
            "gh api --paginate repos/internalcorp/private-svc/issues -f body=hello",
        ],
        ids=[
            "glab-put-input-quoted",
            "glab-x-put-input",
            "gh-post-field",
            "glab-note-post",
            "gh-leading-slash",
            "gh-boolean-flag-before-url",
        ],
    )
    def test_api_write_to_internal_repo_is_skipped(self, tmp_path: Path, write: str) -> None:
        # Over-block guard: an ``api`` WRITE whose URL path itself names a
        # provably-internal repo carries its body only to that private project's
        # surface (e.g. updating a customer MR description). It must skip the
        # public-leak scan instead of forcing the override escape hatch.
        cfg = _config(tmp_path, ["internalcorp"])
        assert publish_destination.gate_skips_destination(write, None, config_path=cfg) is True

    @pytest.mark.parametrize(
        "write",
        [
            'glab api --method PUT "projects/$opp/merge_requests/7562" --input /tmp/body.json',
            "gh api /user -f name=x",
            "glab api projects/souliane%2Fteatree/issues -f title=x",
            "gh api --jq repos/internalcorp/private-svc user/keys -f key=x",
        ],
        ids=["unexpanded-variable", "non-repo-endpoint", "public-repo", "unknown-flag-value-misparse"],
    )
    def test_api_write_without_provable_internal_target_fails_closed(self, tmp_path: Path, write: str) -> None:
        # The carve-out needs the slug from the URL path itself: a shell
        # variable, a non-repo endpoint, or a public repo stays fail-closed.
        # The ``--jq`` row pins the value-misparse hole: a known value-taking
        # flag's VALUE that merely LOOKS like an internal repo path must not
        # stand in for the real (public-surface) endpoint positional.
        cfg = _config(tmp_path, ["internalcorp"])
        assert publish_destination.gate_skips_destination(write, None, config_path=cfg) is False

    def test_unexpanded_variable_slug_unprovable_even_when_probe_answers_private(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The visibility probe is a PROOF source, but an unexpanded ``$var``
        # slug has an unknowable runtime value -- no probe answer about the
        # literal ``$opp`` string can prove anything about the repo the
        # expanded command will actually hit. Even a private-answering probe
        # must leave it PUBLIC (fail-closed).
        cfg = _config(tmp_path, ["internalcorp"])
        monkeypatch.setattr(publish_destination, "slug_is_private", lambda slug: True)
        cmd = 'glab api --method PUT "projects/$opp/merge_requests/7562" --input /tmp/body.json'
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is False


class TestInternalDenylistScoping:
    """#1415 fix: SCAN by default; SKIP only a PROVABLY-internal target (denylist).

    The original over-block fired on a publish to a PRIVATE internal remote the
    user had not declared. The fix is config-driven: with the user's internal
    namespace in ``internal_publish_namespaces`` (the denylist), that internal
    target SKIPS, while EVERY non-internal target -- a genuinely-public
    non-teatree repo, an unknown target, an unresolvable target -- still SCANS.
    The classifier stays FAIL-CLOSED so no public surface can leak unscanned.
    """

    def test_denylisted_internal_target_skips(self, tmp_path: Path) -> None:
        # MUST-NOT-FIRE: the reported over-block, fixed via the denylist. A
        # private internal namespace named in ``internal_publish_namespaces`` is
        # provably internal, so the gate skips.
        cfg = _config(tmp_path, ["internal-eng"])
        cmd = 'glab mr note 5 -R internal-eng/internal-product --message "customercorp note"'
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is True

    def test_user_owned_non_teatree_public_repo_is_scanned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # F3 (the leak path the review caught): a USER-OWNED non-teatree PUBLIC
        # repo (e.g. a blog repo) is NOT in the denylist and the probe confirms it
        # PUBLIC, so it must SCAN. A fail-open allowlist would have skipped it and
        # leaked an internal term onto a public surface.
        cfg = _config(tmp_path, ["internal-eng"])
        monkeypatch.setattr(publish_destination, "slug_is_private", lambda slug: False)
        dest = publish_destination.Destination(slug="ourorg/other-public-repo", via="flag")
        assert publish_destination.is_public_destination(dest, config_path=cfg) is True
        cmd = 'gh issue create -R ourorg/other-public-repo --body "customercorp leak"'
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is False

    def test_unknown_visibility_non_denylisted_target_is_scanned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # MUST-FIRE: a target not in the denylist whose visibility the in-hook
        # probe cannot resolve (the common cold-hook state) stays PUBLIC and is
        # scanned -- detection failure never opens the gate.
        cfg = _config(tmp_path, ["internal-eng"])
        monkeypatch.setattr(publish_destination, "slug_is_private", lambda slug: False)
        cmd = 'gh issue create -R someowner/mystery --body "customercorp note"'
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is False

    def test_unresolvable_target_is_scanned_failsafe(self, tmp_path: Path) -> None:
        # FAIL-SAFE: a publish whose target cannot be resolved from the command
        # at all keeps scanning -- an unparsable target is never a silent bypass.
        cfg = _config(tmp_path, ["internal-eng"])
        cmd = "curl -d x https://example.com"
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is False


class TestForgeAwareVisibility:
    """The probe must use the PUBLISH TOOL's forge, not guess from the bare slug.

    A ``glab`` post always targets GitLab and a ``gh`` post always targets
    GitHub. Resolving a bare ``owner/repo`` slug's forge from its host segment
    (which a bare slug lacks) defaulted every flagless GitLab target to the
    GitHub probe, so an internal/private GitLab MR (``glab mr create -R
    ns/repo``) was probed via ``gh``, never confirmed private, and the gate
    over-fired. Threading the tool's forge to the probe is the fix: the gate
    must SKIP a private GitLab/GitHub target resolved purely by the live probe
    (no allowlist entry), while a genuinely-public GitHub repo still scans.
    """

    def test_destination_records_glab_forge(self) -> None:
        dest = publish_destination.resolve_publish_destination("glab mr create -R internalcorp/svc --title x")
        assert dest is not None
        assert dest.forge == "gitlab"

    def test_destination_records_gh_forge(self) -> None:
        dest = publish_destination.resolve_publish_destination("gh pr create -R someowner/repo --title x")
        assert dest is not None
        assert dest.forge == "github"

    def test_private_gitlab_target_uses_glab_probe_and_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # MUST-NOT-FIRE: a bare GitLab slug from ``glab mr create`` must be
        # probed via ``glab`` (the tool's forge) and skipped when private, even
        # with NO allowlist entry. The bug probed it via ``gh`` -> None -> PUBLIC.
        cfg = _config(tmp_path, [])
        calls: list[tuple[str, str]] = []

        def fake_glab(repo_path: str) -> str:
            calls.append(("glab", repo_path))
            return "PRIVATE"

        def fake_gh(repo_path: str) -> None:
            calls.append(("gh", repo_path))

        monkeypatch.setattr(_repo_visibility, "_probe_glab", fake_glab)
        monkeypatch.setattr(_repo_visibility, "_probe_gh", fake_gh)
        monkeypatch.setattr(_repo_visibility, "_read_visibility_cache", lambda *_a, **_k: None)
        monkeypatch.setattr(_repo_visibility, "_write_visibility_cache", lambda *_a, **_k: None)

        cmd = "glab mr create -R internalcorp/private-svc --title x --description y"
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is True
        assert ("glab", "internalcorp/private-svc") in calls
        assert ("gh", "internalcorp/private-svc") not in calls

    def test_private_github_target_uses_gh_probe_and_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # MUST-NOT-FIRE: a private GitHub repo, resolved purely by the ``gh``
        # probe with no allowlist entry, must skip the public-leak scan.
        cfg = _config(tmp_path, [])
        monkeypatch.setattr(_repo_visibility, "_probe_gh", lambda repo_path: "PRIVATE")
        monkeypatch.setattr(_repo_visibility, "_probe_glab", lambda repo_path: None)
        monkeypatch.setattr(_repo_visibility, "_read_visibility_cache", lambda *_a, **_k: None)
        monkeypatch.setattr(_repo_visibility, "_write_visibility_cache", lambda *_a, **_k: None)

        cmd = "gh pr create -R someowner/private-repo --title x --body y"
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is True

    def test_public_github_target_still_scans(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # MUST-FIRE: a genuinely-public GitHub repo (souliane/teatree itself) the
        # ``gh`` probe confirms PUBLIC stays SCANNED -- the forge fix must not
        # relax the public-surface default.
        cfg = _config(tmp_path, [])
        monkeypatch.setattr(_repo_visibility, "_probe_gh", lambda repo_path: "PUBLIC")
        monkeypatch.setattr(_repo_visibility, "_probe_glab", lambda repo_path: None)
        monkeypatch.setattr(_repo_visibility, "_read_visibility_cache", lambda *_a, **_k: None)
        monkeypatch.setattr(_repo_visibility, "_write_visibility_cache", lambda *_a, **_k: None)

        cmd = "gh pr create -R souliane/teatree --title x --body y"
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is False


class TestT3ReviewDestination:
    """Fix A: ``t3 [overlay] review post-comment/post-draft-note`` resolves to its repo slug.

    The destination resolver early-returned ``None`` for any non-``gh``/``glab``
    leader, so a ``t3 review post-comment <repo> ...`` segment resolved to no
    destination -- and ``t3`` is not a recognised inert leader either, so the
    whole command fell through to the fail-closed scan. A post to an
    allowlisted-private repo via ``t3 review`` therefore over-fired the
    banned-terms gate. The resolver now extracts the repo positional (the first
    non-flag token after the verb) and classifies it against the internal
    allowlist; ``t3 review`` is GitLab-only, so the forge is pinned to gitlab.
    """

    def test_resolve_post_comment_records_slug_via_and_forge(self) -> None:
        dest = publish_destination.resolve_publish_destination(
            "t3 review post-comment internalcorp/svc 6378 --body-file /x --live"
        )
        assert dest is not None
        assert dest.slug == "internalcorp/svc"
        assert dest.via == "t3"
        assert dest.forge == "gitlab"

    def test_resolve_post_draft_note_records_slug(self) -> None:
        dest = publish_destination.resolve_publish_destination(
            "t3 review post-draft-note internalcorp/svc 6378 --body-file /x"
        )
        assert dest is not None
        assert dest.slug == "internalcorp/svc"
        assert dest.via == "t3"
        assert dest.forge == "gitlab"

    def test_resolve_tolerates_interleaved_leading_flag_before_repo(self) -> None:
        # The repo is the first NON-FLAG positional after the verb, so a leading
        # boolean flag interleaved before it does not derail resolution.
        dest = publish_destination.resolve_publish_destination(
            "t3 review post-comment --live internalcorp/svc 6378 --body-file /x"
        )
        assert dest is not None
        assert dest.slug == "internalcorp/svc"

    def test_non_review_t3_verb_is_none(self) -> None:
        # A ``t3`` sub-command that is not a review post verb resolves to no
        # destination via this path (the resolver only knows the review posts).
        assert publish_destination.resolve_publish_destination("t3 review list internalcorp/svc") is None

    def test_internal_post_comment_is_skipped(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["internalcorp"])
        assert (
            publish_destination.gate_skips_destination(
                "t3 review post-comment internalcorp/svc 6378 --body-file /x --live", None, config_path=cfg
            )
            is True
        )

    def test_internal_post_draft_note_is_skipped(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["internalcorp"])
        assert (
            publish_destination.gate_skips_destination(
                "t3 review post-draft-note internalcorp/svc 6378 --body-file /x", None, config_path=cfg
            )
            is True
        )

    def test_overlay_token_between_t3_and_review_is_tolerated(self, tmp_path: Path) -> None:
        # The arbitrary overlay token between ``t3`` and ``review`` must not
        # break detection (e.g. ``t3 acme-internal review post-comment ...``).
        cfg = _config(tmp_path, ["internalcorp"])
        assert (
            publish_destination.gate_skips_destination(
                "t3 acme-internal review post-comment internalcorp/svc 6378 --body-file /x --live",
                None,
                config_path=cfg,
            )
            is True
        )

    def test_path_form_t3_leader_is_canonicalised(self, tmp_path: Path) -> None:
        # A path-form leader (``./t3``) canonicalises to the ``t3`` basename, the
        # same as ``_segment_is_t3_publish`` does.
        cfg = _config(tmp_path, ["internalcorp"])
        assert (
            publish_destination.gate_skips_destination(
                "./t3 review post-comment internalcorp/svc 6378 --body-file /x --live", None, config_path=cfg
            )
            is True
        )

    def test_chained_cd_then_post_comment_is_skipped(self, tmp_path: Path) -> None:
        # A leading ``cd <wt>`` is a recognised inert leader, and the trailing
        # ``t3 review post-comment`` segment resolves to a provably-internal repo.
        cfg = _config(tmp_path, ["internalcorp"])
        cmd = "cd /wt && t3 review post-comment internalcorp/svc 6378 --body-file /x --live"
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is True

    def test_public_repo_post_still_scans(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # MUST-FIRE: a ``t3 review`` post to a NON-allowlisted repo the probe
        # cannot prove private stays PUBLIC and is scanned -- recognising the
        # destination must not relax the public-surface default.
        cfg = _config(tmp_path, ["internalcorp"])
        monkeypatch.setattr(publish_destination, "slug_is_private", lambda slug: False)
        cmd = "t3 review post-comment public-org/app 6378 --body-file /x --live"
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is False


class TestApiEndpointNormalization:
    """Fix B: ``api/vN/`` and full-URL ``gh``/``glab api`` endpoints resolve to a slug.

    ``_destination_from_api`` matched only a RELATIVE ``repos/...`` /
    ``projects/...`` path, so an ``api/v4/projects/<ns>%2F<repo>/...`` endpoint
    or a full ``https://gitlab.com/api/v4/projects/...`` URL yielded ``None`` and
    the gate over-blocked an internal-MR update. The endpoint is now normalised
    (a leading ``https?://<host>/`` and an ``api/vN/`` segment stripped) before
    the existing relative patterns -- purely additive, a public endpoint still
    resolves public and still scans.
    """

    def test_resolve_versioned_projects_path(self) -> None:
        dest = publish_destination.resolve_publish_destination(
            "glab api --method POST api/v4/projects/internalcorp%2Fsvc/merge_requests/6378/notes -f body=x"
        )
        assert dest is not None
        assert dest.slug == "internalcorp/svc"
        assert dest.via == "api"

    def test_resolve_full_url_projects_path(self) -> None:
        dest = publish_destination.resolve_publish_destination(
            "glab api --method POST "
            "https://gitlab.com/api/v4/projects/internalcorp%2Fsvc/merge_requests/6378/notes -f body=x"
        )
        assert dest is not None
        assert dest.slug == "internalcorp/svc"
        assert dest.via == "api"

    def test_resolve_gh_full_url_repos_path(self) -> None:
        # The same normalisation strips the GitHub API host so a full-URL ``gh
        # api`` endpoint resolves its ``repos/owner/repo`` slug too.
        dest = publish_destination.resolve_publish_destination(
            "gh api --method POST https://api.github.com/repos/internalcorp/svc/issues -f body=x"
        )
        assert dest is not None
        assert dest.slug == "internalcorp/svc"
        assert dest.via == "api"

    def test_internal_versioned_write_is_skipped(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["internalcorp"])
        cmd = "glab api --method POST api/v4/projects/internalcorp%2Fsvc/merge_requests/6378/notes -f body=x"
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is True

    def test_internal_full_url_write_is_skipped(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, ["internalcorp"])
        cmd = (
            "glab api --method POST "
            "https://gitlab.com/api/v4/projects/internalcorp%2Fsvc/merge_requests/6378/notes -f body=x"
        )
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is True

    def test_public_versioned_write_still_scans(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, ["internalcorp"])
        monkeypatch.setattr(publish_destination, "slug_is_private", lambda slug: False)
        cmd = "glab api --method POST api/v4/projects/public-org%2Fapp/merge_requests/6378/notes -f body=x"
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is False

    def test_public_full_url_write_still_scans(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, ["internalcorp"])
        monkeypatch.setattr(publish_destination, "slug_is_private", lambda slug: False)
        cmd = (
            "glab api --method POST "
            "https://gitlab.com/api/v4/projects/public-org%2Fapp/merge_requests/6378/notes -f body=x"
        )
        assert publish_destination.gate_skips_destination(cmd, None, config_path=cfg) is False


def _private_repos_config(tmp_path: Path, namespaces: list[str]) -> Path:
    cfg = tmp_path / ".teatree.toml"
    entries = ", ".join(f'"{n}"' for n in namespaces)
    cfg.write_text(f"[teatree]\nprivate_repos = [{entries}]\n", encoding="utf-8")
    return cfg


class TestRestrictedPathCwdResolution:
    """The cwd-remote slug must resolve OFFLINE inside the restricted hook PATH.

    The PreToolUse hook subprocess inherits a PATH that frequently does not
    resolve a bare ``git``. Resolving the flagless-create destination by
    shelling out to ``git remote get-url`` failed there, so the destination
    resolved to ``None`` and the banned-terms gate OVER-BLOCKED a flagless
    ``glab mr create`` to the user's OWN private repo (the offline
    ``private_repos`` allowlist never got a slug to match). The slug is now
    parsed from ``.git/config`` directly, so it resolves with no ``git`` on
    PATH -- while a genuinely-PUBLIC cwd still scans (no over-relaxation).
    """

    def test_flagless_create_to_private_cwd_skips_without_git_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # MUST-NOT-FIRE (red on revert): a flagless ``glab mr create`` whose cwd
        # is an allowlisted-private checkout SKIPS the leak scan even when ``git``
        # is unresolvable on PATH -- the offline ``.git/config`` parse supplies
        # the slug the allowlist matches. Before the fix the slug was empty, the
        # destination None, and the gate over-blocked.
        repo = _repo_with_remote(tmp_path / "wt", "git@gitlab.com:internalcorp/svc.git")
        cfg = _private_repos_config(tmp_path, ["internalcorp"])
        monkeypatch.setenv("PATH", "")  # mimic the restricted hook subprocess: no git
        cmd = "glab mr create --source-branch x --target-branch master --fill"
        assert publish_destination.gate_skips_destination(cmd, repo, config_path=cfg) is True

    def test_flagless_create_from_linked_worktree_skips_without_git_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The real-world shape: the agent's cwd is a LINKED worktree whose
        # ``.git`` is a FILE pointing at the shared common-dir config. The slug
        # must still resolve offline so the private post is not over-blocked.
        repo = _repo_with_remote(tmp_path / "main", "git@gitlab.com:internalcorp/svc.git")
        _git(repo, "commit", "--allow-empty", "-m", "init")
        linked = tmp_path / "linked"
        _git(repo, "worktree", "add", str(linked), "-b", "feat/x")
        cfg = _private_repos_config(tmp_path, ["internalcorp"])
        monkeypatch.setenv("PATH", "")
        cmd = "glab mr create --source-branch x --target-branch master --fill"
        assert publish_destination.gate_skips_destination(cmd, linked, config_path=cfg) is True

    def test_flagless_create_to_public_cwd_still_scans_without_git_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # MUST-FIRE (no over-relaxation): the offline slug resolution must NOT
        # relax a genuinely-PUBLIC cwd. With the repo not in the allowlist and
        # the probe confirming PUBLIC, the flagless create still scans.
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:souliane/teatree.git")
        cfg = _private_repos_config(tmp_path, ["internalcorp"])
        monkeypatch.setattr(publish_destination, "slug_is_private", lambda slug: False)
        monkeypatch.setenv("PATH", "")
        cmd = "gh pr create --title x --body y"
        assert publish_destination.gate_skips_destination(cmd, repo, config_path=cfg) is False
