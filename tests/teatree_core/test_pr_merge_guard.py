"""#764 local-squash merge — forced deterministic author for public souliane/*.

Server-side ``gh pr merge --squash`` authors the squash commit with the
merging account's email regardless of local git identity. The fix
performs the squash LOCALLY and forces the author + committer to the
canonical noreply identity, so the result is deterministic independent
of any account/config. The post-push ``gh api`` author check is retained
as fail-closed defense-in-depth. Private / non-souliane repos keep the
server-side ``gh pr merge`` path unchanged. Only the git/gh subprocess
boundaries are mocked; the helper logic + noreply regex are real.
"""

from unittest.mock import patch

import pytest

from teatree.core.pr_merge import squash_merge_public
from teatree.core.public_identity import MergeAuthorMismatchError, canonical_noreply_identity


class _Recorder:
    def __init__(self, origin: str = "git@github.com:souliane/teatree.git") -> None:
        self.git: list[tuple[list[str], dict[str, str], str | None]] = []
        self.gh: list[list[str]] = []
        self.landed_author = "21343492+souliane@users.noreply.github.com"
        self.push_rc = 0
        self.switch_rc = 0
        self.origin = origin

    def run_git(
        self,
        args: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> tuple[int, str, str]:
        self.git.append((args, dict(env or {}), cwd))
        if args[:3] == ["remote", "get-url", "origin"]:
            return (0, f"{self.origin}\n", "")
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return (0, "/clones/souliane/teatree\n", "")
        if args[:1] == ["push"]:
            return (self.push_rc, "", "rejected: protected branch" if self.push_rc else "")
        if args[:2] == ["switch", "main"]:
            return (self.switch_rc, "", "main is checked out elsewhere" if self.switch_rc else "")
        if args[:2] == ["rev-parse", "HEAD"]:
            return (0, "abc1234deadbeefcafe\n", "")
        return (0, "", "")

    def run_gh(self, argv: list[str]) -> tuple[int, str, str]:
        self.gh.append(argv)
        if "view" in argv and "mergeCommit" in " ".join(argv):
            return (0, "", "")
        if "view" in argv:
            return (0, "feat(x): a thing\n\nbody line\n", "")
        if "api" in argv:
            return (0, f"{self.landed_author}\n", "")
        return (0, "", "")


def _patch(rec: _Recorder, *, visibility: str = "PUBLIC"):
    # #785: the public/private branch in squash_merge_public now gates
    # on visibility (`gh repo view --json visibility`), not a hardcoded
    # owner — mock the only unstoppable external (the gh subprocess).
    def fake_gh_visibility(cmd: list[str], **_kw: object) -> object:
        del cmd
        return type("R", (), {"stdout": visibility + "\n", "returncode": 0})()

    return (
        patch("teatree.core.pr_merge._run_git", side_effect=rec.run_git),
        patch("teatree.core.pr_merge._run_gh", side_effect=rec.run_gh),
        patch("teatree.core.public_identity.run_allowed_to_fail", side_effect=fake_gh_visibility),
    )


class TestLocalSquashMergePublic:
    def test_authors_commit_locally_with_forced_noreply_identity(self) -> None:
        rec = _Recorder()
        p_git, p_gh, p_vis = _patch(rec)
        with p_git, p_gh, p_vis:
            squash_merge_public(pr=764, slug="souliane/teatree")

        name, email = canonical_noreply_identity()
        assert any(a[:2] == ["merge", "--squash"] for a, _, _ in rec.git), rec.git
        commit_args, commit_env, commit_cwd = next((a, e, c) for a, e, c in rec.git if a[:1] == ["commit"])
        assert f"--author={name} <{email}>" in commit_args, commit_args
        assert commit_env.get("GIT_COMMITTER_NAME") == name
        assert commit_env.get("GIT_COMMITTER_EMAIL") == email
        # F1: every mutating git op is pinned to the resolved clone cwd,
        # never the (arbitrary) invocation dir.
        assert commit_cwd == "/clones/souliane/teatree"
        push = next((a, c) for a, _, c in rec.git if a[:2] == ["push", "origin"])
        assert push[1] == "/clones/souliane/teatree"
        assert not any("merge" in g and "--squash" in g for g in rec.gh), (
            "must NOT use server-side gh pr merge --squash on public souliane/*"
        )

    def test_refuses_when_origin_is_not_the_target_slug(self) -> None:
        # F1: invoked from a clone whose origin is a DIFFERENT repo —
        # must refuse before any mutating op (no fetch/merge/commit/push).
        rec = _Recorder(origin="git@github.com:someoneelse/otherrepo.git")
        p_git, p_gh, p_vis = _patch(rec)
        with p_git, p_gh, p_vis, pytest.raises(RuntimeError, match=r"(?i)origin|wrong repo|refus"):
            squash_merge_public(pr=764, slug="souliane/teatree")
        assert not any(a[:1] in (["merge"], ["commit"], ["push"]) for a, _, _ in rec.git), (
            "must not mutate when origin != target slug"
        )

    def test_repo_path_pins_all_git_ops_to_that_clone(self) -> None:
        # F1-adjacent (bootstrap + unattended sweeps from arbitrary cwd):
        # --repo-path makes every git op target that clone, and the
        # origin assertion runs THERE, not the process cwd.
        rec = _Recorder()
        p_git, p_gh, p_vis = _patch(rec)
        clone = "/clones/souliane/teatree"
        with p_git, p_gh, p_vis:
            squash_merge_public(pr=764, slug="souliane/teatree", repo_path=clone)

        origin_cwd = next(c for a, _, c in rec.git if a[:3] == ["remote", "get-url", "origin"])
        toplevel_cwd = next(c for a, _, c in rec.git if a[:2] == ["rev-parse", "--show-toplevel"])
        # The safety checks (origin-assert, toplevel-resolve) run in the
        # PINNED clone, never the arbitrary process cwd ".".
        assert origin_cwd == clone, rec.git
        assert toplevel_cwd == clone, rec.git

    def test_switch_main_failure_stops_before_squash(self) -> None:
        # F2: main checked out elsewhere -> switch fails -> STOP, never
        # squash/commit/push on the wrong branch.
        rec = _Recorder()
        rec.switch_rc = 1
        p_git, p_gh, p_vis = _patch(rec)
        with p_git, p_gh, p_vis, pytest.raises(RuntimeError, match=r"(?i)switch|main|elsewhere"):
            squash_merge_public(pr=764, slug="souliane/teatree")
        assert not any(a[:2] == ["merge", "--squash"] for a, _, _ in rec.git), (
            "must not squash after a failed switch to main"
        )
        assert not any(a[:1] == ["commit"] for a, _, _ in rec.git)

    def test_fails_closed_when_landed_author_is_non_noreply(self) -> None:
        rec = _Recorder()
        rec.landed_author = "real.dev@internal.example"
        p_git, p_gh, p_vis = _patch(rec)
        with p_git, p_gh, p_vis, pytest.raises(MergeAuthorMismatchError):
            squash_merge_public(pr=999, slug="souliane/teatree")

    def test_protected_branch_push_rejection_stops_no_force(self) -> None:
        rec = _Recorder()
        rec.push_rc = 1
        p_git, p_gh, p_vis = _patch(rec)
        with p_git, p_gh, p_vis, pytest.raises(RuntimeError, match=r"(?i)push|protected|reject"):
            squash_merge_public(pr=764, slug="souliane/teatree")
        assert not any("--force" in a or "--force-with-lease" in a for a, _, _ in rec.git), (
            "must not force-push as a workaround"
        )

    def test_private_repo_uses_server_side_merge_unchanged(self) -> None:
        rec = _Recorder()
        p_git, p_gh, p_vis = _patch(rec, visibility="PRIVATE")
        with p_git, p_gh, p_vis:
            squash_merge_public(pr=1, slug="acme-private/internal-svc")

        assert any("merge" in g and "--squash" in g for g in rec.gh), rec.gh
        assert not any(a[:2] == ["merge", "--squash"] for a, _, _ in rec.git)
        assert not any(a[:1] == ["commit"] for a, _, _ in rec.git)


class TestPrMergeCommandWiring:
    def test_command_delegates_to_squash_merge_public(self) -> None:
        from django.core.management import call_command  # noqa: PLC0415

        with patch("teatree.core.pr_merge.squash_merge_public") as helper:
            result = call_command("pr", "merge", "764", "souliane/teatree")

        helper.assert_called_once_with(pr=764, slug="souliane/teatree", repo_path="", auto=False)
        assert result == {"merged": True, "pr": 764, "slug": "souliane/teatree", "auto": False}
