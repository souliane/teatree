"""Integration tests for the ``t3 fast-push`` engine (user directive #8).

Real git repos under ``tmp_path`` with a local bare ``origin``; only the
forge CLI (network) is faked. The secret used in fixtures is assembled at
runtime so this test file never contains a literal matchable token.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from teatree.core.fast_push import LEAK_GATES, FastPusher, FastPushOutcome, GhForge, GlabForge, forge_for_repo
from teatree.utils.run import run_checked


@dataclass
class FakeForge:
    existing_pr_url: str = ""
    created: list[dict[str, str]] = field(default_factory=list)
    updated: list[dict[str, str]] = field(default_factory=list)

    def find_pr_url(self, *, branch: str) -> str:
        return self.existing_pr_url

    def create_pr(self, *, branch: str, title: str, body: str) -> str:
        self.created.append({"branch": branch, "title": title, "body": body})
        return "https://example.invalid/pr/1"

    def update_pr(self, *, url: str, body: str) -> None:
        self.updated.append({"url": url, "body": body})


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    run_checked(["git", "init", "--bare", str(origin)])
    work = tmp_path / "work"
    run_checked(["git", "init", "-b", "main", str(work)])
    run_checked(["git", "config", "user.email", "agent@users.noreply.github.com"], cwd=work)
    run_checked(["git", "config", "user.name", "agent"], cwd=work)
    run_checked(["git", "remote", "add", "origin", str(origin)], cwd=work)
    (work / "README.md").write_text("seed\n")
    run_checked(["git", "add", "-A"], cwd=work)
    run_checked(["git", "commit", "-m", "seed"], cwd=work)
    run_checked(["git", "push", "-u", "origin", "main"], cwd=work)
    run_checked(["git", "checkout", "-b", "feature"], cwd=work)
    return work


@pytest.fixture
def leak_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_BANNED_TERMS", "forbiddenbrand")
    monkeypatch.setenv("TEATREE_OVERLAY_LEAK_TERMS", "secretoverlay")


def run_fast_push(repo: Path, forge: FakeForge, **kwargs: str) -> FastPushOutcome:
    return FastPusher(repo=repo, forge=forge, **kwargs).run()


class TestLeakGatesRefuse:
    def test_refuses_staged_banned_term(self, repo: Path, leak_env: None) -> None:
        (repo / "notes.md").write_text("mentions forbiddenbrand here\n")
        forge = FakeForge()

        outcome = run_fast_push(repo, forge)

        assert not outcome.ok
        assert any(f.gate == "banned-terms" and f.path == "notes.md" for f in outcome.findings)
        assert any("forbiddenbrand" in f.detail for f in outcome.findings)
        assert not outcome.committed
        assert not outcome.pushed
        assert forge.created == []

    def test_refuses_staged_secret(self, repo: Path, leak_env: None) -> None:
        planted = "ghp" + "_" + "a1b2c3d4e5f6a7b8c9d0"
        (repo / "config.py").write_text(f'TOKEN = "{planted}"\n')
        forge = FakeForge()

        outcome = run_fast_push(repo, forge)

        assert not outcome.ok
        assert any(f.gate == "secret-scan" for f in outcome.findings)
        assert not outcome.pushed

    def test_refuses_staged_overlay_term(self, repo: Path, leak_env: None) -> None:
        (repo / "core.py").write_text("client = 'secretoverlay'\n")
        forge = FakeForge()

        outcome = run_fast_push(repo, forge)

        assert not outcome.ok
        assert any(f.gate == "overlay-leak" and f.path == "core.py" for f in outcome.findings)
        assert not outcome.pushed

    def test_refuses_banned_term_in_message(self, repo: Path, leak_env: None) -> None:
        (repo / "clean.py").write_text("x = 1\n")
        forge = FakeForge()

        outcome = run_fast_push(repo, forge, message="feat: mention forbiddenbrand")

        assert not outcome.ok
        assert not outcome.committed

    def test_fails_closed_when_banned_terms_unconfigured(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("T3_BANNED_TERMS", raising=False)
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        (repo / "clean.py").write_text("x = 1\n")
        forge = FakeForge()

        outcome = run_fast_push(repo, forge)

        assert not outcome.ok
        assert any(f.gate == "banned-terms" and "unset" in f.detail.lower() for f in outcome.findings)
        assert not outcome.pushed


class TestCleanPush:
    def test_pushes_and_creates_pr(self, repo: Path, leak_env: None) -> None:
        (repo / "feature.py").write_text("x = 1\n")
        forge = FakeForge()

        outcome = run_fast_push(repo, forge, message="feat: clean change", remaining="wire the CLI flag")

        assert outcome.ok
        assert outcome.committed
        assert outcome.pushed
        assert outcome.pr_action == "created"
        assert outcome.pr_url == "https://example.invalid/pr/1"
        remote_heads = run_checked(["git", "ls-remote", "--heads", "origin", "feature"], cwd=repo).stdout
        assert "refs/heads/feature" in remote_heads
        assert forge.created[0]["title"] == "feat: clean change"
        assert "REMAINING:" in forge.created[0]["body"]
        assert "wire the CLI flag" in forge.created[0]["body"]

    def test_updates_existing_pr(self, repo: Path, leak_env: None) -> None:
        (repo / "feature.py").write_text("x = 1\n")
        forge = FakeForge(existing_pr_url="https://example.invalid/pr/7")

        outcome = run_fast_push(repo, forge, message="feat: clean change")

        assert outcome.ok
        assert outcome.pr_action == "updated"
        assert outcome.pr_url == "https://example.invalid/pr/7"
        assert forge.created == []
        assert forge.updated[0]["url"] == "https://example.invalid/pr/7"

    def test_auto_message_when_none_given(self, repo: Path, leak_env: None) -> None:
        (repo / "feature.py").write_text("x = 1\n")
        forge = FakeForge()

        outcome = run_fast_push(repo, forge)

        assert outcome.ok
        subject = run_checked(["git", "log", "-1", "--format=%s"], cwd=repo).stdout.strip()
        assert "fast-push" in subject
        assert "feature" in subject

    def test_refuses_on_default_branch(self, repo: Path, leak_env: None) -> None:
        run_checked(["git", "checkout", "main"], cwd=repo)
        (repo / "feature.py").write_text("x = 1\n")
        forge = FakeForge()

        outcome = run_fast_push(repo, forge)

        assert not outcome.ok
        assert any("default branch" in f.detail for f in outcome.findings)
        assert not outcome.committed

    def test_refuses_when_default_branch_unresolvable(self, repo: Path, leak_env: None) -> None:
        (repo / "feature.py").write_text("x = 1\n")

        with patch("teatree.core.fast_push.git.default_branch", side_effect=RuntimeError("boom")):
            outcome = run_fast_push(repo, FakeForge())

        assert not outcome.ok
        assert any(f.gate == "branch-guard" and "fail closed" in f.detail for f in outcome.findings)
        assert not outcome.committed
        assert not outcome.pushed


class TestAuthorIdentityGate:
    def test_refuses_non_noreply_identity_on_public_repo(self, repo: Path, leak_env: None) -> None:
        run_checked(["git", "config", "user.email", "dev@example.com"], cwd=repo)
        (repo / "feature.py").write_text("x = 1\n")

        with patch("teatree.core.fast_push._public_github_slug", return_value="souliane/teatree"):
            outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok
        assert any(f.gate == "author-identity" for f in outcome.findings)
        assert any("example.com" in f.detail for f in outcome.findings)
        assert not outcome.committed
        assert not outcome.pushed

    def test_allows_noreply_identity_on_public_repo(self, repo: Path, leak_env: None) -> None:
        (repo / "feature.py").write_text("x = 1\n")

        with patch("teatree.core.fast_push._public_github_slug", return_value="souliane/teatree"):
            outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert outcome.ok
        assert outcome.pushed

    def test_inert_when_not_public_github(self, repo: Path, leak_env: None) -> None:
        run_checked(["git", "config", "user.email", "dev@example.com"], cwd=repo)
        (repo / "feature.py").write_text("x = 1\n")

        with patch("teatree.core.fast_push._public_github_slug", return_value=None):
            outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert outcome.ok
        assert not any(f.gate == "author-identity" for f in outcome.findings)


class TestNonLeakGatesSkipped:
    def test_executes_exactly_the_leak_gate_set(self, repo: Path, leak_env: None) -> None:
        (repo / "feature.py").write_text("x = 1\n")

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert outcome.executed_gates == LEAK_GATES
        assert outcome.executed_gates == ("banned-terms", "secret-scan", "overlay-leak", "author-identity")

    def test_bypasses_repo_hook_chain(self, repo: Path, leak_env: None) -> None:
        hooks = repo / ".git" / "hooks"
        sentinel = repo / "hook-ran.sentinel"
        for name in ("pre-commit", "pre-push"):
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 1\n")
            hook.chmod(0o755)
        (repo / "feature.py").write_text("x = 1\n")

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert outcome.ok
        assert outcome.committed
        assert outcome.pushed
        assert not sentinel.exists()


class TestForgeResolution:
    def test_unknown_remote_skips_pr_but_pushes(self, repo: Path, leak_env: None) -> None:
        (repo / "feature.py").write_text("x = 1\n")

        outcome = FastPusher(repo=repo, message="feat: clean change").run()

        assert outcome.ok
        assert outcome.pushed
        assert outcome.pr_action == "skipped"
        assert outcome.pr_url == ""

    def test_github_remote_resolves_gh(self, repo: Path) -> None:
        run_checked(["git", "remote", "set-url", "origin", "git@github.com:acme/widgets.git"], cwd=repo)
        assert isinstance(forge_for_repo(repo), GhForge)

    def test_gitlab_remote_resolves_glab(self, repo: Path) -> None:
        run_checked(["git", "remote", "set-url", "origin", "https://gitlab.com/acme/widgets.git"], cwd=repo)
        assert isinstance(forge_for_repo(repo), GlabForge)

    def test_no_remote_resolves_none(self, tmp_path: Path) -> None:
        bare = tmp_path / "no-remote"
        run_checked(["git", "init", "-b", "main", str(bare)])
        assert forge_for_repo(bare) is None


class TestForgeCliCommands:
    def _completed(self, stdout: str, returncode: int = 0) -> CompletedProcess[str]:
        return CompletedProcess(args=["stub"], returncode=returncode, stdout=stdout, stderr="")

    def test_gh_find_create_update(self, tmp_path: Path) -> None:
        forge = GhForge(tmp_path)
        with patch("teatree.core.fast_push.run_allowed_to_fail", return_value=self._completed("https://x/pr/4\n")):
            assert forge.find_pr_url(branch="b") == "https://x/pr/4"
        with patch("teatree.core.fast_push.run_allowed_to_fail", return_value=self._completed("", returncode=1)):
            assert forge.find_pr_url(branch="b") == ""
        with patch("teatree.core.fast_push.run_checked", return_value=self._completed("https://x/pr/5\n")) as run:
            assert forge.create_pr(branch="b", title="t", body="d") == "https://x/pr/5"
            forge.update_pr(url="https://x/pr/5", body="d2")
        created_cmd, updated_cmd = run.call_args_list[0].args[0], run.call_args_list[1].args[0]
        assert created_cmd[:3] == ["gh", "pr", "create"]
        assert "--assignee" not in created_cmd
        assert updated_cmd[:3] == ["gh", "pr", "edit"]

    def test_glab_find_create_update(self, tmp_path: Path) -> None:
        forge = GlabForge(tmp_path)
        listing = json.dumps([{"web_url": "https://gl/mr/7"}])
        with patch("teatree.core.fast_push.run_allowed_to_fail", return_value=self._completed(listing)):
            assert forge.find_pr_url(branch="b") == "https://gl/mr/7"
        with patch("teatree.core.fast_push.run_allowed_to_fail", return_value=self._completed("not-json")):
            assert forge.find_pr_url(branch="b") == ""
        with patch(
            "teatree.core.fast_push.run_checked", return_value=self._completed("created https://gl/mr/8\n")
        ) as run:
            assert forge.create_pr(branch="b", title="t", body="d") == "https://gl/mr/8"
            forge.update_pr(url="https://gl/mr/8", body="d2")
        assert run.call_args_list[1].args[0][:4] == ["glab", "mr", "update", "8"]
