"""Integration tests for the ``t3 fast-push`` engine (user directive #8).

Real git repos under ``tmp_path`` with a local bare ``origin``; only the
forge CLI (network) is faked. The secret used in fixtures is assembled at
runtime so this test file never contains a literal matchable token.
"""

import json
import secrets
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from teatree.core.authoring_credential import UnapprovableAuthorError, reset_authoring_credential_cache
from teatree.core.forge_pr_probe import PrProbe
from teatree.core.overlay import OverlayBase, OverlayConfig
from teatree.core.push.fast_push import (
    EMPTY_DELTA_PR_SKIP,
    LEAK_GATES,
    UNAPPROVABLE_AUTHOR_PR_REFUSAL,
    FastPusher,
    FastPushOutcome,
    GhForge,
    GlabForge,
    LeakGateScan,
    forge_for_repo,
)
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.hooks import _repo_visibility
from teatree.hooks._repo_visibility import slug_for_remote_url
from teatree.utils.run import CommandFailedError, run_checked


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
    monkeypatch.setenv(
        "TEATREE_TERM_REGISTRY",
        json.dumps({"leak": ["forbiddenbrand"], "prose_collider": ["forbiddenbrand"], "overlay": ["secretoverlay"]}),
    )


@pytest.fixture(autouse=True)
def _no_visibility_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No forge CLI to probe with, and a private verdict cache — so origin's visibility is undetermined."""
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "viscache"))


def run_fast_push(repo: Path, forge: FakeForge, **kwargs: str) -> FastPushOutcome:
    return FastPusher(repo=repo, forge=forge, **kwargs).run()


class TestLeakGatesRefuse:
    def test_refuses_staged_banned_term(self, repo: Path, leak_env: None) -> None:
        (repo / "notes.md").write_text("mentions forbiddenbrand here\n")
        forge = FakeForge()

        outcome = run_fast_push(repo, forge)

        assert not outcome.ok
        assert any(f.gate == "banned-terms" and f.path == "notes.md" for f in outcome.findings)
        assert any(f.detail == "banned term leak#0" for f in outcome.findings)
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
        monkeypatch.delenv("TEATREE_TERM_REGISTRY", raising=False)
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

        with patch("teatree.core.push.fast_push.git.default_branch", side_effect=RuntimeError("boom")):
            outcome = run_fast_push(repo, FakeForge())

        assert not outcome.ok
        assert any(f.gate == "branch-guard" and "fail closed" in f.detail for f in outcome.findings)
        assert not outcome.committed
        assert not outcome.pushed


class TestAuthorIdentityGate:
    def test_refuses_non_noreply_identity_on_public_repo(self, repo: Path, leak_env: None) -> None:
        run_checked(["git", "config", "user.email", "dev@example.com"], cwd=repo)
        (repo / "feature.py").write_text("x = 1\n")

        with patch("teatree.core.push.fast_push.visibility_for_remote", return_value="PUBLIC"):
            outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok
        assert any(f.gate == "author-identity" for f in outcome.findings)
        assert any("example.com" in f.detail for f in outcome.findings)
        assert not outcome.committed
        assert not outcome.pushed

    def test_allows_noreply_identity_on_public_repo(self, repo: Path, leak_env: None) -> None:
        (repo / "feature.py").write_text("x = 1\n")

        with patch("teatree.core.push.fast_push.visibility_for_remote", return_value="PUBLIC"):
            outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert outcome.ok
        assert outcome.pushed

    @pytest.mark.parametrize("verdict", ["PRIVATE", "INTERNAL"])
    def test_inert_when_origin_is_known_non_public(self, repo: Path, leak_env: None, verdict: str) -> None:
        run_checked(["git", "config", "user.email", "dev@example.com"], cwd=repo)
        (repo / "feature.py").write_text("x = 1\n")

        with patch("teatree.core.push.fast_push.visibility_for_remote", return_value=verdict):
            outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert outcome.ok
        assert not any(f.gate == "author-identity" for f in outcome.findings)

    def test_refuses_non_noreply_identity_when_visibility_is_undetermined(self, repo: Path, leak_env: None) -> None:
        """The pre-push hook this lane bypasses treats an unconfirmed visibility as public; so must the lane."""
        run_checked(["git", "config", "user.email", "dev@example.com"], cwd=repo)
        (repo / "feature.py").write_text("x = 1\n")

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok
        assert any(f.gate == "author-identity" and "example.com" in f.detail for f in outcome.findings)
        assert not outcome.committed
        assert not outcome.pushed

    def test_an_unprobeable_github_origin_is_treated_as_public(
        self, repo: Path, leak_env: None, tmp_path: Path
    ) -> None:
        run_checked(["git", "remote", "set-url", "origin", "https://github.com/octo/mystery.git"], cwd=repo)
        run_checked(["git", "remote", "set-url", "--push", "origin", str(tmp_path / "origin.git")], cwd=repo)
        run_checked(["git", "config", "user.email", "dev@example.com"], cwd=repo)
        (repo / "feature.py").write_text("x = 1\n")

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok
        finding = next(f for f in outcome.findings if f.gate == "author-identity")
        assert "octo/mystery" in finding.detail
        assert "could not be confirmed" in finding.detail
        assert not outcome.pushed


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
    @pytest.fixture(autouse=True)
    def _routed_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def resolve(_repo: str, *, credential: str) -> ForgeTokenResolution:
            return ForgeTokenResolution(credential, "test", ForgeTokenState.TOKEN, token="routed-token")

        monkeypatch.setattr("teatree.core.forge_pr_probe.resolve_repo_token", resolve)
        monkeypatch.setattr("teatree.core.push.fast_push.resolve_repo_token", resolve)

    def _completed(self, stdout: str, returncode: int = 0) -> CompletedProcess[str]:
        return CompletedProcess(args=["stub"], returncode=returncode, stdout=stdout, stderr="")

    def test_gh_find_create_update(self, tmp_path: Path) -> None:
        forge = GhForge(tmp_path)
        listing = json.dumps([{"url": "https://x/pr/4"}])
        with patch("teatree.core.forge_pr_probe.run_allowed_to_fail", return_value=self._completed(listing)):
            assert forge.find_pr_url(branch="b") == "https://x/pr/4"
        with patch("teatree.core.forge_pr_probe.run_allowed_to_fail", return_value=self._completed("", returncode=1)):
            assert forge.find_pr_url(branch="b") == ""
        with patch("teatree.core.push.fast_push.run_checked", return_value=self._completed("https://x/pr/5\n")) as run:
            assert forge.create_pr(branch="b", title="t", body="d") == "https://x/pr/5"
            forge.update_pr(url="https://x/pr/5", body="d2")
        created_cmd, updated_cmd = run.call_args_list[0].args[0], run.call_args_list[1].args[0]
        assert created_cmd[:3] == ["gh", "pr", "create"]
        assert "--assignee" not in created_cmd
        assert updated_cmd[:3] == ["gh", "pr", "edit"]

    def test_glab_find_create_update(self, tmp_path: Path) -> None:
        # ``find_pr_url`` reads over the GitLab HTTP API, not ``glab``: the deploy image
        # declares no ``glab``, yet a bind-mounted ``~/.local/bin`` may still supply one, so a
        # probe must not rest on absence — the writes asserted below still shell out to it.
        forge = GlabForge(tmp_path)
        with patch("teatree.core.forge_pr_probe._gitlab_open_mr_url", return_value="https://gl/mr/7"):
            assert forge.find_pr_url(branch="b") == "https://gl/mr/7"
        with patch("teatree.core.forge_pr_probe._gitlab_open_mr_url", return_value=None):
            assert forge.find_pr_url(branch="b") == ""
        with patch(
            "teatree.core.push.fast_push.run_checked", return_value=self._completed("created https://gl/mr/8\n")
        ) as run:
            assert forge.create_pr(branch="b", title="t", body="d") == "https://gl/mr/8"
            forge.update_pr(url="https://gl/mr/8", body="d2")
        assert run.call_args_list[1].args[0][:4] == ["glab", "mr", "update", "8"]


class TestCoreGateClassRouting:
    """The fast-push gate is a ``core``-scope gate and must request core classes.

    ``GATE_CLASSES["core"]`` deliberately excludes the diff-only ``tone`` class.
    Routing this gate through the ``diff`` union would silently widen fast-push
    beyond the registry's own per-gate contract the moment a registry is
    populated.
    """

    @staticmethod
    def _seed_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = tmp_path / "registry.sqlite3"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_term_registry', ?)",
            (json.dumps({"leak": ["acme"], "prose_collider": [], "tone": ["blunder"]}),),
        )
        conn.commit()
        conn.close()
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        monkeypatch.delenv("TEATREE_TERM_REGISTRY", raising=False)

    def test_leak_class_term_is_flagged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._seed_registry(tmp_path, monkeypatch)
        findings = LeakGateScan._banned_terms({"sample.txt": ["acme"]})
        assert [f.detail for f in findings] == ["banned term leak#0"]

    def test_tone_class_term_is_not_flagged_by_the_core_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_registry(tmp_path, monkeypatch)
        assert LeakGateScan._banned_terms({"sample.txt": ["blunder"]}) == []

    @staticmethod
    def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: dict[str, list[str]]) -> None:
        db = tmp_path / "registry.sqlite3"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute("DELETE FROM teatree_config_setting WHERE key = 'banned_term_registry'")
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_term_registry', ?)",
            (json.dumps(registry),),
        )
        conn.commit()
        conn.close()
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        monkeypatch.delenv("TEATREE_TERM_REGISTRY", raising=False)

    def test_overlay_gate_reads_the_registry_overlay_class(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With no overlay env override, the overlay gate must resolve through the
        # registry's ``overlay`` class (not the excluded ``leak``/``prose_collider``).
        self._seed(tmp_path, monkeypatch, {"leak": ["democorp"], "prose_collider": [], "overlay": ["acme-internal"]})
        findings = LeakGateScan._overlay_leak({"sample.txt": ["uses acme-internal here"]})
        assert [f.detail for f in findings] == ["overlay-scoped term overlay#0"]
        assert LeakGateScan._overlay_leak({"sample.txt": ["mentions democorp"]}) == []

    def test_a_term_shaped_like_an_email_or_an_id_is_named_by_class_and_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        email_term = f"zq{secrets.token_hex(4)}@qx{secrets.token_hex(3)}.com"
        id_term = "C0" + "".join(secrets.choice("BDFGHJKPQRSVWXZ0123456789") for _ in range(9))
        self._seed(tmp_path, monkeypatch, {"leak": [email_term], "prose_collider": [], "overlay": [id_term]})

        details = [f.detail for f in LeakGateScan._banned_terms({"s.txt": [f"mail {email_term}"]})]
        details += [f.detail for f in LeakGateScan._overlay_leak({"s.txt": [f"channel {id_term}"]})]

        assert details == ["banned term leak#0", "overlay-scoped term overlay#0", "opaque id overlay#0"]

    def test_banned_terms_carve_out_reads_the_registry_allow_class(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The company-identifier carve-out must come from the registry's ``allow``
        # class: an allow-listed identifier is blanked before matching, so the
        # ``prose_collider`` slug inside it is NOT flagged.
        self._seed(tmp_path, monkeypatch, {"leak": [], "prose_collider": ["acme"], "allow": ["acme-product"]})
        assert LeakGateScan._banned_terms({"sample.txt": ["the acme-product repo"]}) == []
        # Remove the carve-out and the bare slug flags again — anti-vacuous control.
        self._seed(tmp_path, monkeypatch, {"leak": [], "prose_collider": ["acme"]})
        assert [f.detail for f in LeakGateScan._banned_terms({"sample.txt": ["the acme-product repo"]})] == [
            "banned term prose_collider#0"
        ]


def _commit(repo: Path, filename: str, content: str, message: str, *, email: str = "") -> None:
    (repo / filename).write_text(content)
    run_checked(["git", "add", "-A"], cwd=repo)
    cmd = ["git", "-c", f"user.email={email}"] if email else ["git"]
    run_checked([*cmd, "commit", "-m", message], cwd=repo)


def _merge_forward(repo: Path, *, main_content: str, main_email: str = "") -> None:
    """Advance ``main`` with *main_content*, publish it, and merge it into ``feature``.

    The #3523 shape: everything the merge carries in is ALREADY public, so a range that
    re-judges it turns every ordinary merge-forward into a refusal.
    """
    run_checked(["git", "checkout", "main"], cwd=repo)
    _commit(repo, "prior.txt", main_content, "prior PR merged on main", email=main_email)
    run_checked(["git", "push", "origin", "main"], cwd=repo)
    run_checked(["git", "checkout", "feature"], cwd=repo)
    run_checked(["git", "merge", "origin/main", "-m", "merge origin/main into feature"], cwd=repo)


class TestTheGatesScanThePushRangeNotJustTheStagedDelta:
    """``git push --no-verify`` delivers every committed-but-unpushed commit.

    Three of the four gates read only ``git diff --cached``, so a secret committed in
    an earlier turn — with nothing staged now — was pushed with ``ok: True`` and
    ``findings: []``, all four gates reporting executed. The bash hook this engine
    replaces (``scripts/hooks/refuse-public-push-with-leak.sh``) has always scanned
    the whole push range.
    """

    def test_a_secret_in_a_committed_unpushed_commit_is_refused(self, repo: Path, leak_env: None) -> None:
        planted = "ghp" + "_" + "a1b2c3d4e5f6a7b8c9d0"
        _commit(repo, "config.py", f'TOKEN = "{planted}"\n', "chore: wip checkpoint")
        forge = FakeForge()

        outcome = run_fast_push(repo, forge)

        assert not outcome.ok
        assert any(f.gate == "secret-scan" for f in outcome.findings), outcome.findings
        assert not outcome.pushed
        assert (
            "refs/heads/feature"
            not in run_checked(["git", "ls-remote", "--heads", "origin", "feature"], cwd=repo).stdout
        )

    def test_a_banned_term_in_an_unpushed_commit_message_is_refused(self, repo: Path, leak_env: None) -> None:
        # The hook judges commit MESSAGES in the range too — they reach public history
        # exactly like file content. `_banned_terms` saw only the pending message.
        _commit(repo, "clean.py", "x = 1\n", "feat: wire up forbiddenbrand support")
        forge = FakeForge()

        outcome = run_fast_push(repo, forge)

        assert not outcome.ok
        assert any(f.gate == "banned-terms" for f in outcome.findings), outcome.findings
        assert not outcome.pushed

    def test_a_banned_term_in_an_unpushed_commit_diff_is_refused(self, repo: Path, leak_env: None) -> None:
        _commit(repo, "notes.md", "mentions forbiddenbrand here\n", "docs: notes")
        forge = FakeForge()

        outcome = run_fast_push(repo, forge)

        assert not outcome.ok
        assert any(f.gate == "banned-terms" for f in outcome.findings), outcome.findings
        assert not outcome.pushed

    def test_a_non_noreply_identity_on_an_unpushed_commit_is_refused(self, repo: Path, leak_env: None) -> None:
        _commit(repo, "clean.py", "x = 1\n", "feat: clean", email="dev@example.com")

        with patch("teatree.core.push.fast_push.visibility_for_remote", return_value="PUBLIC"):
            outcome = run_fast_push(repo, FakeForge())

        assert not outcome.ok
        assert any(f.gate == "author-identity" and "example.com" in f.detail for f in outcome.findings)
        assert not outcome.pushed

    def test_a_push_range_git_cannot_walk_is_a_hard_refusal(self, repo: Path, leak_env: None) -> None:
        # A range nothing can bound is refused, never skipped: `git log` failing is a scan that did not happen.
        (repo / "feature.py").write_text("x = 1\n")

        with patch("teatree.core.push.push_range._git_output", return_value=None):
            outcome = run_fast_push(repo, FakeForge())

        assert not outcome.ok
        assert any(f.gate == "push-range" for f in outcome.findings), outcome.findings
        assert not outcome.committed
        assert not outcome.pushed


class TestThePushRangeDoesNotReJudgeAlreadyPublicHistory:
    """The port of ``_remote_sha_is_trusted_base`` + the hook's merge-forward handling.

    Naive range scanning re-judges ``main``'s own history, so every merge-forward push
    refuses. The range is HEAD minus every already-public tip — ``origin/<default>`` and,
    when the branch exists on the remote, its own tip — never a linear span (#3523).
    """

    def test_a_merge_forward_carrying_a_finding_on_main_is_allowed(self, repo: Path, leak_env: None) -> None:
        _merge_forward(repo, main_content="a prior PR mentioning forbiddenbrand\n")
        (repo / "feature.py").write_text("x = 1\n")

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert outcome.ok, outcome.findings
        assert outcome.pushed

    def test_a_merge_forward_does_not_re_judge_mains_commit_identities(self, repo: Path, leak_env: None) -> None:
        _merge_forward(repo, main_content="a clean prior line\n", main_email="squash@example.com")
        (repo / "feature.py").write_text("x = 1\n")

        with patch("teatree.core.push.fast_push.visibility_for_remote", return_value="PUBLIC"):
            outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert outcome.ok, outcome.findings
        assert not any("squash@example.com" in f.detail for f in outcome.findings)

    def test_a_branch_finding_on_top_of_a_merge_forward_still_refuses(self, repo: Path, leak_env: None) -> None:
        _merge_forward(repo, main_content="a clean prior line\n")
        _commit(repo, "leak.md", "mentions forbiddenbrand here\n", "docs: add notes")

        outcome = run_fast_push(repo, FakeForge())

        assert not outcome.ok
        assert any(f.gate == "banned-terms" for f in outcome.findings), outcome.findings

    def test_an_already_pushed_branch_commit_is_not_re_judged(self, repo: Path, leak_env: None) -> None:
        # The branch's own remote tip is a public tip too: a commit already on
        # `origin/feature` is not newly exposed by this push, so it is out of range.
        _commit(repo, "old.md", "mentions forbiddenbrand here\n", "docs: forbiddenbrand notes")
        run_checked(["git", "push", "-u", "origin", "feature"], cwd=repo)
        (repo / "feature.py").write_text("x = 1\n")

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert outcome.ok, outcome.findings
        assert outcome.pushed


class TestThePushRangeNeverTrustsALocalTrackingRef:
    """``t3 fast-push`` bypasses the hook, so its own range has to be as hard to empty as the hook's.

    A remote-tracking ref is a file the pusher can write, and one that says ``origin/main`` already holds the
    unpushed leak empties the range. The range is what the remote lists at the URL the push goes to, from the same
    ``teatree.hooks.remote_holds`` the pre-push hook runs.
    """

    def _leak_committed(self, repo: Path) -> None:
        _commit(repo, "notes.md", "mentions forbiddenbrand here\n", "docs: notes")

    def test_a_planted_tracking_ref_does_not_empty_the_range(self, repo: Path, leak_env: None) -> None:
        self._leak_committed(repo)
        head = run_checked(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        run_checked(["git", "update-ref", "refs/remotes/origin/main", head], cwd=repo)
        run_checked(["git", "update-ref", "refs/remotes/origin/feature", head], cwd=repo)

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, "the planted refs emptied the range"
        assert any(f.gate == "banned-terms" for f in outcome.findings), outcome.findings
        assert not outcome.pushed

    def test_a_url_rewrite_that_splits_the_listing_from_the_push_does_not_empty_the_range(
        self, repo: Path, leak_env: None, tmp_path: Path
    ) -> None:
        self._leak_committed(repo)
        origin = tmp_path / "origin.git"
        fake = tmp_path / "fake.git"
        run_checked(["git", "clone", "--bare", str(repo), str(fake)])
        run_checked(["git", "config", f"url.{fake}.insteadOf", str(origin)], cwd=repo)
        run_checked(["git", "config", f"url.{origin}.pushInsteadOf", str(origin)], cwd=repo)

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, "the listing read the fake repository while the push went to the real one"
        assert any(f.gate == "banned-terms" for f in outcome.findings), outcome.findings

    def test_a_remote_that_cannot_be_listed_widens_the_range(self, repo: Path, leak_env: None, tmp_path: Path) -> None:
        _commit(repo, "old.md", "mentions forbiddenbrand here\n", "docs: forbiddenbrand notes")
        run_checked(["git", "push", "-u", "origin", "feature"], cwd=repo)
        run_checked(["git", "remote", "set-url", "origin", str(tmp_path / "no-such-repository")], cwd=repo)

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, "history the remote already holds is re-judged when the remote cannot say so"
        assert any(f.gate == "banned-terms" for f in outcome.findings), outcome.findings

    def test_a_planted_private_cache_entry_does_not_waive_the_identity_gate(
        self, repo: Path, leak_env: None, tmp_path: Path
    ) -> None:
        origin = tmp_path / "origin.git"
        run_checked(["git", "remote", "set-url", "origin", "git@github.com:acme/widget.git"], cwd=repo)
        run_checked(["git", "remote", "set-url", "--push", "origin", str(origin)], cwd=repo)
        cache = tmp_path / "viscache"
        cache.mkdir(exist_ok=True)
        entry = {"ts": time.time(), "visibility": "PRIVATE"}
        (cache / "repo-visibility-cache.json").write_text(
            json.dumps({slug_for_remote_url("git@github.com:acme/widget.git"): entry}), encoding="utf-8"
        )
        _commit(repo, "clean.py", "x = 1\n", "feat: clean", email="dev@example.com")

        outcome = run_fast_push(repo, FakeForge())

        assert not outcome.ok, "a cached PRIVATE waived the identity gate"
        assert any(f.gate == "author-identity" and "example.com" in f.detail for f in outcome.findings)


class TestRepoLocalGitStateDoesNotBlankWhatFastPushReads:
    """A textconv driver or a replace ref lives in ``.git`` and changes what git shows, tracked files untouched."""

    @staticmethod
    def _blank_textconv(repo: Path, tmp_path: Path, pattern: str) -> None:
        blank = tmp_path / "blank.sh"
        blank.write_text("#!/bin/sh\nexit 0\n")
        blank.chmod(0o755)
        run_checked(["git", "config", "diff.blank.textconv", str(blank)], cwd=repo)
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "attributes").write_text(f"{pattern} diff=blank\n")

    def test_a_textconv_driver_does_not_blank_the_committed_range(
        self, repo: Path, leak_env: None, tmp_path: Path
    ) -> None:
        _commit(repo, "notes.md", "mentions forbiddenbrand here\n", "docs: notes")
        self._blank_textconv(repo, tmp_path, "notes.md")

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, "the converted patch emptied what the gates read"
        assert any(f.gate == "banned-terms" for f in outcome.findings), outcome.findings
        assert not outcome.pushed

    def test_a_textconv_driver_does_not_blank_the_staged_diff(self, repo: Path, leak_env: None, tmp_path: Path) -> None:
        self._blank_textconv(repo, tmp_path, "notes.md")
        (repo / "notes.md").write_text("mentions forbiddenbrand here\n")

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, "the converted staged diff emptied what the gates read"
        assert {f.gate for f in outcome.findings} >= {"banned-terms", "secret-scan"}, outcome.findings
        assert not outcome.pushed

    def test_a_replaced_commit_does_not_hide_the_range(self, repo: Path, leak_env: None) -> None:
        _commit(repo, "notes.md", "mentions forbiddenbrand here\n", "docs: notes")
        leak = run_checked(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        clean = run_checked(
            ["git", "commit-tree", f"{leak}~1^{{tree}}", "-p", f"{leak}~1", "-m", "a clean stand-in"], cwd=repo
        ).stdout.strip()
        run_checked(["git", "replace", leak, clean], cwd=repo)
        (repo / "notes.md").unlink()

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, "the replacement commit hid the leak from the range"
        assert not outcome.pushed

    def test_a_replaced_commit_does_not_hide_a_header_of_its_object(self, repo: Path, leak_env: None) -> None:
        leak = _hand_made_commit(repo, _HandMade(author="forbiddenbrand"))
        clean = run_checked(
            ["git", "commit-tree", f"{leak}^{{tree}}", "-p", f"{leak}~1", "-m", "a clean stand-in"], cwd=repo
        ).stdout.strip()
        run_checked(["git", "replace", leak, clean], cwd=repo)

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, "the replacement commit hid the header of the object from the whole-object read"
        assert not outcome.pushed


class TestACommitMessageIsReadAsStoredAndAsDecoded:
    """A commit's ``encoding`` header travels with the pushed object, so what git decodes is not all the remote holds.

    ``i18n.commitEncoding=UTF-16LE`` on plain bytes makes ``git log`` re-decode them into noise while the pushed object
    still carries the message; a message stored in Shift_JIS is logged in Shift_JIS unless the read asks for UTF-8, and
    its stored bytes read ``second-leak-A-term`` (0x83 0x41 is "ア"), which only the UTF-8 read matches.
    """

    @staticmethod
    def _commit_message(repo: Path, tmp_path: Path, message: bytes, *config: str) -> None:
        (repo / "a.txt").write_text("a clean line\n")
        run_checked(["git", "add", "a.txt"], cwd=repo)
        message_file = tmp_path / "message.txt"
        message_file.write_bytes(message)
        subprocess.run(  # bytes, not text: git echoes the subject in the commit's own encoding
            [str(shutil.which("git")), *config, "commit", "--cleanup=verbatim", "-F", str(message_file)],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    def test_a_wrong_encoding_header_does_not_hide_a_leaking_message(
        self, repo: Path, leak_env: None, tmp_path: Path
    ) -> None:
        self._commit_message(
            repo, tmp_path, b"release notes mention forbiddenbrand here\n", "-c", "i18n.commitEncoding=UTF-16LE"
        )

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, "the message landed behind a wrong encoding header"
        assert any(f.gate == "banned-terms" for f in outcome.findings), outcome.findings
        assert not outcome.pushed

    def test_a_message_stored_in_another_encoding_is_read_as_git_decodes_it(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": ["second-leak-term"], "prose_collider": []}))
        run_checked(["git", "config", "i18n.commitEncoding", "Shift_JIS"], cwd=repo)
        self._commit_message(repo, tmp_path, "notes mention second-leak-ア-term here\n".encode("shift_jis"))

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, "git logged the message in Shift_JIS and the read did not ask for UTF-8"
        assert any(f.gate == "banned-terms" for f in outcome.findings), outcome.findings
        assert not outcome.pushed


class TestATermInAPathOrABranchNameIsRefused:
    """A registry term can also ride out as a file path (new, renamed, deleted) or as the branch name."""

    @pytest.mark.parametrize("change", ["new", "rename", "delete"])
    def test_a_path_with_a_term_is_refused(self, repo: Path, leak_env: None, change: str) -> None:
        if change == "new":
            (repo / "forbiddenbrand.txt").write_text("a clean line\n")
        elif change == "rename":
            run_checked(["git", "mv", "README.md", "forbiddenbrand.md"], cwd=repo)
        else:
            _commit(repo, "forbiddenbrand.txt", "a clean line\n", "docs: add a file")
            run_checked(["git", "push", "origin", "feature"], cwd=repo)
            (repo / "forbiddenbrand.txt").unlink()

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, f"a {change} path with a term landed"
        assert not outcome.pushed

    def test_a_branch_name_with_a_term_is_refused(self, repo: Path, leak_env: None) -> None:
        run_checked(["git", "checkout", "-b", "forbiddenbrand-fix"], cwd=repo)
        (repo / "feature.py").write_text("x = 1\n")

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, "a branch name with a term landed"
        assert not outcome.pushed


@dataclass(frozen=True)
class _HandMade:
    """What a hand-made commit object carries beyond ``git commit``'s own output."""

    author: str = "agent"
    committer: str = "agent"
    author_email: str = ""
    committer_email: str = ""
    header: str = ""
    blank_line: bool = False
    second_parent: str = ""


def _hand_made_commit(repo: Path, plant: _HandMade | None = None) -> str:
    """Write a commit object by hand on ``HEAD``'s tree and move the branch onto it; the object may end in a header."""
    plant = plant or _HandMade()
    config_email = run_checked(["git", "config", "user.email"], cwd=repo).stdout.strip()
    tree = run_checked(["git", "rev-parse", "HEAD^{tree}"], cwd=repo).stdout.strip()
    parents = [run_checked(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()]
    parents += [plant.second_parent] if plant.second_parent else []
    lines = [f"tree {tree}", *(f"parent {parent}" for parent in parents)]
    lines += [f"author {plant.author} <{plant.author_email or config_email}> 1700000000 +0000"]
    lines += [f"committer {plant.committer} <{plant.committer_email or config_email}> 1700000000 +0000"]
    text = "\n".join(lines) + "\n"
    text += f"{plant.header}\n" if plant.header else ""
    text += "\na clean message\n" if (plant.blank_line or not plant.header) else ""
    sha = run_checked(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=repo, stdin_text=text).stdout.strip()
    run_checked(["git", "update-ref", "refs/heads/feature", sha], cwd=repo)
    return sha


#: A secret-shaped login: the identity rule accepts any ``[A-Za-z0-9-]+@users.noreply.github.com``, and ``sk-`` fits it.
_DISGUISED = "sk" + "-" + "A" * 24 + "@users.noreply.github.com"


class TestACommitObjectIsReadWholeNotOnlyItsMessage:
    """A commit object carries more text than its message: extra headers and the author and committer names.

    Those travel with the pushed object and no CI job reads commit objects, so the registry-term gates read each whole
    object and the generic scan reads it with only the noreply domain cut: a noreply address is no finding, while a
    secret or a home path in a header, or in the login before that domain, is refused.
    """

    @pytest.mark.parametrize(
        "plant",
        [
            {"header": "x-note mentions forbiddenbrand"},
            {"author": "forbiddenbrand"},
            {"committer": "forbiddenbrand"},
        ],
        ids=["extra-header", "author-name", "committer-name"],
    )
    def test_a_term_in_a_header_is_refused(self, repo: Path, leak_env: None, plant: dict[str, str]) -> None:
        _hand_made_commit(repo, _HandMade(**plant))

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, "the term landed in a commit header"
        assert any(f.gate == "banned-terms" for f in outcome.findings), outcome.findings
        assert not outcome.pushed

    @pytest.mark.parametrize("shape", ["with-a-message", "without-a-blank-line"])
    @pytest.mark.parametrize(
        ("header", "category"),
        [
            ("x-note token " + "glpat" + "-" + "X" * 16, "api_key"),
            ("x-note see " + "/home/" + "someone" + "/secret", "home_path"),
        ],
        ids=["secret", "home-path"],
    )
    def test_a_secret_or_a_home_path_in_a_header_is_refused(
        self, repo: Path, leak_env: None, header: str, category: str, shape: str
    ) -> None:
        """Every detector reads the object, not only the term gates; the same value in a message is refused."""
        _hand_made_commit(repo, _HandMade(header=header, blank_line=shape == "with-a-message"))

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, "the value landed in a commit header"
        assert any(f.gate == "secret-scan" and category in f.detail for f in outcome.findings), outcome.findings
        assert not outcome.pushed

    def test_a_clean_merge_commit_passes(self, repo: Path, leak_env: None) -> None:
        """Anti-vacuity: a merge has two ``parent`` lines and a noreply committer, and is no finding either."""
        _commit(repo, "own.py", "y = 0\n", "feat: a clean change of the branch's own")
        _merge_forward(repo, main_content="a clean prior line\n")
        (repo / "feature.py").write_text("x = 1\n")

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert outcome.ok, outcome.findings

    @pytest.mark.parametrize(
        "plant",
        [
            _HandMade(author_email=_DISGUISED),
            _HandMade(committer_email=_DISGUISED),
            _HandMade(header=f"author y <{_DISGUISED}> 1700000000 +0000"),
            _HandMade(
                header="\n".join(
                    [
                        "gpgsig -----BEGIN PGP SIGNATURE-----",
                        f" author y <{_DISGUISED}> 1700000000 +0000",
                        " -----END PGP SIGNATURE-----",
                    ]
                )
            ),
        ],
        ids=["author", "committer", "duplicate-author-line", "line-mimicking-an-identity-inside-a-signature"],
    )
    def test_a_secret_shaped_login_is_refused_though_the_domain_is_noreply(
        self, repo: Path, leak_env: None, plant: _HandMade
    ) -> None:
        """Only the exact ``@users.noreply.github.com`` is hidden from the detectors, not the login in front of it."""
        _hand_made_commit(repo, plant)

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert not outcome.ok, "the token landed as a noreply login"
        assert any(f.gate == "secret-scan" and "api_key" in f.detail for f in outcome.findings), outcome.findings
        assert not outcome.pushed

    def test_a_clean_merge_of_a_signed_tag_passes(self, repo: Path, leak_env: None) -> None:
        """Git copies the tag's ``tagger`` line, indented, into a ``mergetag`` header: a noreply address, no finding."""
        email = run_checked(["git", "config", "user.email"], cwd=repo).stdout.strip()
        run_checked(["git", "checkout", "-b", "side"], cwd=repo)
        _commit(repo, "side.txt", "a clean side line\n", "side work")
        side = run_checked(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        run_checked(["git", "checkout", "feature"], cwd=repo)
        mergetag = "\n".join(
            [
                f"mergetag object {side}",
                " type commit",
                " tag v1",
                f" tagger Dev <{email}> 1700000000 +0000",
                " ",
                " a clean signed tag",
                " -----BEGIN PGP SIGNATURE-----",
                " ",
                " PLACEHOLDERSIGNATUREBLOCKNOTAREALSIGNATURE",
                " -----END PGP SIGNATURE-----",
            ]
        )
        _hand_made_commit(repo, _HandMade(second_parent=side, header=mergetag, blank_line=True))

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert outcome.ok, outcome.findings

    def test_a_clean_hand_made_commit_passes(self, repo: Path, leak_env: None) -> None:
        """Anti-vacuity: the same object shape with nothing in it is not a finding, noreply identities included."""
        _hand_made_commit(repo)

        outcome = run_fast_push(repo, FakeForge(), message="feat: clean change")

        assert outcome.ok, outcome.findings
        assert outcome.pushed


def _branch_work_lands_on_main_then_merges_back(repo: Path) -> None:
    """The #4429 shape: the branch's content reaches ``main`` under a different SHA.

    ``feature`` is then ahead of ``origin/main`` by SHA and by nothing else, so the
    pull request it would open carries zero files.
    """
    content = "def parse(text):\n    return text.strip()\n"
    _commit(repo, "parse.py", content, "feat: parse")
    run_checked(["git", "checkout", "main"], cwd=repo)
    _commit(repo, "parse.py", content, "feat: parse (#1)")
    run_checked(["git", "push", "origin", "main"], cwd=repo)
    run_checked(["git", "checkout", "feature"], cwd=repo)
    run_checked(["git", "merge", "origin/main", "-m", "merge origin/main into feature"], cwd=repo)


class TestEmptyDeltaPrGuard:
    """#4551: the third PR-opening chokepoint on the class #4550 closed two doors of."""

    def test_no_pr_is_created_when_the_branch_delta_is_empty(self, repo: Path, leak_env: None) -> None:
        _branch_work_lands_on_main_then_merges_back(repo)
        forge = FakeForge()

        outcome = run_fast_push(repo, forge, message="chore: checkpoint")

        assert forge.created == []
        assert outcome.pr_action == EMPTY_DELTA_PR_SKIP
        assert outcome.pr_url == ""

    def test_the_push_still_lands_when_the_pr_is_skipped(self, repo: Path, leak_env: None) -> None:
        """The checkpoint is the point: a withheld pull request must never strand the work."""
        _branch_work_lands_on_main_then_merges_back(repo)

        outcome = run_fast_push(repo, FakeForge(), message="chore: checkpoint")

        assert outcome.ok, outcome.findings
        assert outcome.pushed
        remote_heads = run_checked(["git", "ls-remote", "--heads", "origin", "feature"], cwd=repo).stdout
        assert "refs/heads/feature" in remote_heads

    def test_a_branch_carrying_real_content_still_gets_a_pr(self, repo: Path, leak_env: None) -> None:
        """The paired positive — a guard that refused everything would pass its own negative test."""
        (repo / "feature.py").write_text("x = 1\n")
        forge = FakeForge()

        outcome = run_fast_push(repo, forge, message="feat: clean change")

        assert outcome.pr_action == "created"
        assert outcome.pr_skip_reason == ""
        assert len(forge.created) == 1

    def test_an_unreadable_probe_still_opens_the_pr(self, repo: Path, leak_env: None) -> None:
        """Fail CLOSED: a probe that could not run never suppresses a pull request the branch owes."""
        _branch_work_lands_on_main_then_merges_back(repo)
        forge = FakeForge()
        unreadable = CommandFailedError(["git", "diff"], 128, "", "fatal: bad revision")

        with patch("teatree.core.worktree.branch_landed.git.run_strict", side_effect=unreadable):
            outcome = run_fast_push(repo, forge, message="chore: checkpoint")

        assert outcome.pr_action == "created"
        assert len(forge.created) == 1

    def test_an_existing_pr_is_still_updated_on_an_empty_delta(self, repo: Path, leak_env: None) -> None:
        """The guard gates CREATION: the open pull request exists either way, so its body stays current."""
        _branch_work_lands_on_main_then_merges_back(repo)
        forge = FakeForge(existing_pr_url="https://example.invalid/pr/7")

        outcome = run_fast_push(repo, forge, message="chore: checkpoint")

        assert outcome.pr_action == "updated"
        assert forge.created == []
        assert forge.updated[0]["url"] == "https://example.invalid/pr/7"

    def test_the_skip_reason_names_a_remedy_and_claims_no_unproven_cause(self, repo: Path, leak_env: None) -> None:
        """A three-dot probe proves the delta is empty, never WHY — so the refusal may not name a cause."""
        _branch_work_lands_on_main_then_merges_back(repo)

        outcome = run_fast_push(repo, FakeForge(), message="chore: checkpoint")

        assert "t3 fast-push" in outcome.pr_skip_reason
        assert "commit it on 'feature'" in outcome.pr_skip_reason
        assert "squash-merge" not in outcome.pr_skip_reason


class TestThePushedBranchNameIsScanned:
    """``git push --no-verify`` skips the pre-push hook that scans the ref NAME.

    The name is published the moment the push lands and survives branch deletion
    in ``refs/pull/*``, so this lane has to scan it itself or it becomes the way
    around the hook's ref-name gate. Every case passes an explicit clean message:
    the auto-generated one embeds the branch name, so a refusal under
    ``<commit-message>`` would prove nothing about the NAME being scanned.
    """

    def test_refuses_a_banned_term_in_the_branch_name(self, repo: Path, leak_env: None) -> None:
        run_checked(["git", "checkout", "-b", "feat/forbiddenbrand-onboarding"], cwd=repo)
        _commit(repo, "notes.md", "clean\n", "chore: clean")

        outcome = run_fast_push(repo, FakeForge(), message="chore: checkpoint")

        assert not outcome.ok
        assert any(
            f.gate == "banned-terms" and f.path == "<ref-name>" and f.detail == "banned term leak#0"
            for f in outcome.findings
        ), outcome.findings
        assert not outcome.pushed
        assert outcome.executed_gates == LEAK_GATES

    def test_a_clean_branch_name_is_not_a_finding(self, repo: Path, leak_env: None) -> None:
        _commit(repo, "notes.md", "clean\n", "chore: clean")

        outcome = run_fast_push(repo, FakeForge(), message="chore: checkpoint")

        assert outcome.ok, outcome.findings
        assert not any(f.path == "<ref-name>" for f in outcome.findings)

    def test_an_overlay_term_in_the_branch_name_is_refused(self, repo: Path, leak_env: None) -> None:
        run_checked(["git", "checkout", "-b", "wip/secretoverlay-spike"], cwd=repo)
        _commit(repo, "notes.md", "clean\n", "chore: clean")

        outcome = run_fast_push(repo, FakeForge(), message="chore: checkpoint")

        assert not outcome.ok
        assert any(f.gate == "overlay-leak" and f.path == "<ref-name>" for f in outcome.findings), outcome.findings

    def test_a_secret_shaped_branch_name_is_refused_by_the_secret_scan(self, repo: Path, leak_env: None) -> None:
        """The hook runs the whole privacy scanner over the name, so this lane must too."""
        planted = "ghp" + "_" + "a1b2c3d4e5f6a7b8c9d0"
        run_checked(["git", "checkout", "-b", f"wip/{planted}"], cwd=repo)
        _commit(repo, "notes.md", "clean\n", "chore: clean")

        outcome = run_fast_push(repo, FakeForge(), message="chore: checkpoint")

        assert not outcome.ok
        assert any(f.gate == "secret-scan" and f.path == "<ref-name>" for f in outcome.findings), outcome.findings


class TestThePublishedRefIsTheScannedRef:
    """The ref-name gate scans the CURRENT BRANCH; the push must publish that same name.

    ``git push -u origin <branch>`` resolves its destination through git config, so
    two settings let a name the gate never saw reach the remote: a
    ``remote.origin.push`` refspec remaps the destination, and ``push.followTags``
    publishes tags alongside it. Both defeat only this ``--no-verify`` lane — git
    hands the pre-push hook the real destination ref — which is the lane the
    ref-name scan exists to close. Each setting is pinned on its own so one cannot
    regress behind the other.
    """

    def _remote_refs(self, repo: Path) -> str:
        return run_checked(["git", "ls-remote", "origin"], cwd=repo).stdout

    def test_a_remapped_push_refspec_cannot_publish_an_unscanned_name(self, repo: Path, leak_env: None) -> None:
        run_checked(
            ["git", "config", "remote.origin.push", "refs/heads/feature:refs/heads/forbiddenbrand-dest"],
            cwd=repo,
        )
        _commit(repo, "notes.md", "clean\n", "chore: clean")

        outcome = run_fast_push(repo, FakeForge(), message="chore: checkpoint")

        assert outcome.ok, outcome.findings
        assert outcome.pushed
        refs = self._remote_refs(repo)
        assert "refs/heads/feature" in refs, refs
        assert "forbiddenbrand-dest" not in refs, f"an unscanned ref name was published: {refs}"

    def test_follow_tags_cannot_publish_an_unscanned_tag_name(self, repo: Path, leak_env: None) -> None:
        run_checked(["git", "config", "push.followTags", "true"], cwd=repo)
        _commit(repo, "notes.md", "clean\n", "chore: clean")
        run_checked(["git", "tag", "-a", "forbiddenbrand-tag", "-m", "tag"], cwd=repo)

        outcome = run_fast_push(repo, FakeForge(), message="chore: checkpoint")

        assert outcome.ok, outcome.findings
        assert outcome.pushed
        refs = self._remote_refs(repo)
        assert "refs/heads/feature" in refs, refs
        assert "refs/tags/" not in refs, f"an unscanned tag name was published: {refs}"


class TestASyntheticPathNameDoesNotDisplaceARealFile:
    """The gates key findings by path, and the synthetic paths are ordinary dict keys.

    ``<ref-name>`` and ``<commit-message>`` are legal filenames, so a repo holding
    one collided with the synthetic entry: assigning the key dropped the real
    file's lines before any gate read them, and its banned term was pushed.
    """

    def test_a_real_file_named_ref_name_is_still_scanned(self, repo: Path, leak_env: None) -> None:
        _commit(repo, "<ref-name>", "forbiddenbrand\n", "chore: add file")

        outcome = run_fast_push(repo, FakeForge(), message="chore: checkpoint")

        assert not outcome.ok
        assert any(
            f.gate == "banned-terms" and f.path == "<ref-name>" and f.detail == "banned term leak#0"
            for f in outcome.findings
        ), outcome.findings
        assert not outcome.pushed

    def test_a_real_file_named_commit_message_is_still_scanned(self, repo: Path, leak_env: None) -> None:
        _commit(repo, "<commit-message>", "forbiddenbrand\n", "chore: add file")

        outcome = run_fast_push(repo, FakeForge(), message="chore: checkpoint")

        assert not outcome.ok
        assert any(
            f.gate == "banned-terms" and f.path == "<commit-message>" and f.detail == "banned term leak#0"
            for f in outcome.findings
        ), outcome.findings
        assert not outcome.pushed


OWNER_TOKEN = "owner-token"
BOT_TOKEN = "bot-token"
FACTORY_REMOTE = "git@gitlab.com:org/group/factory.git"
PLAIN_REMOTE = "git@gitlab.com:org/product.git"
MR_URL = "https://gitlab.com/org/group/factory/-/merge_requests/9"


class _DeclaresABot(OverlayConfig):
    """An overlay routing the factory remote — and only it — to a bot credential that may not resolve."""

    def __init__(self, *, bot: str = BOT_TOKEN) -> None:
        super().__init__()
        self._bot = bot

    def get_gitlab_token(self) -> str:
        return OWNER_TOKEN

    def get_gitlab_token_for_remote(self, remote: str) -> str:
        return self._bot if "group/factory" in remote else OWNER_TOKEN


class _Glab:
    """Stands in for ``glab``: answers who a token authenticates as, records every call's argv and token.

    The login follows the token the call runs under, so a read-back made under a different token
    than the create's reads as a different identity. Anything that is not ``glab`` (the ``git``
    steps of a whole fast-push) runs for real.
    """

    def __init__(self, logins: dict[str, str] | None = None) -> None:
        self._logins = logins or {BOT_TOKEN: "the-bot", OWNER_TOKEN: "the-owner"}
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> CompletedProcess[str]:
        if cmd[0] != "glab":
            return run_checked(cmd, **kwargs)
        token = kwargs["env"]["GITLAB_TOKEN"]
        self.calls.append((cmd, token))
        if cmd[:3] == ["glab", "api", "user"]:
            return CompletedProcess(cmd, 0, json.dumps({"username": self._logins[token]}), "")
        return CompletedProcess(cmd, 0, f"created {MR_URL}\n", "")

    def tokens_that(self, *verb: str) -> list[str]:
        return [token for cmd, token in self.calls if cmd[: len(verb)] == list(verb)]


class TestGlabForgeAuthorsUnderTheRepoCredential:
    """An MR on a bot-authored repo is opened as the bot, or not at all.

    ``glab mr create`` runs under whatever ``GITLAB_TOKEN`` the caller sets, so the identity
    was the overlay-wide OWNER slot for every repo: ``t3 fast-push`` and the sub-agent barrier
    of ``t3 handover create`` opened MRs the owner's account could then never approve.
    """

    @pytest.fixture(autouse=True)
    def _owner_token_and_approvers(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        def owner(_repo: str, *, credential: str) -> ForgeTokenResolution:
            return ForgeTokenResolution(credential, "test", ForgeTokenState.TOKEN, token=OWNER_TOKEN)

        monkeypatch.setattr("teatree.core.push.fast_push.resolve_repo_token", owner)
        monkeypatch.setattr("teatree.core.authoring_credential.approver_identities", lambda: frozenset({"the-owner"}))
        self._monkeypatch = monkeypatch
        reset_authoring_credential_cache()
        yield
        reset_authoring_credential_cache()

    def _forge(self, repo: Path, remote: str, *configs: OverlayConfig) -> GlabForge:
        bare = run_checked(["git", "remote", "get-url", "origin"], cwd=repo).stdout.strip()
        run_checked(["git", "remote", "set-url", "origin", remote], cwd=repo)
        run_checked(["git", "config", f"url.{bare}.pushInsteadOf", remote], cwd=repo)
        overlays = {}
        for index, config in enumerate(configs):
            overlays[f"overlay-{index}"] = MagicMock(spec=OverlayBase, config=config)
        self._monkeypatch.setattr("teatree.core.authoring_credential.get_all_overlays", lambda: overlays)
        return GlabForge(repo)

    def _whole_fast_push(self, repo: Path, forge: GlabForge, glab: _Glab) -> FastPushOutcome:
        (repo / "feature.py").write_text("x = 1\n")
        with (
            patch("teatree.core.push.fast_push.run_checked", glab),
            patch("teatree.core.push.fast_push.probe_gitlab_open_pr", return_value=PrProbe.none()),
        ):
            return FastPusher(repo=repo, forge=forge, message="feat: clean change").run()

    def test_the_create_runs_under_the_bot_token_not_the_owners(self, repo: Path) -> None:
        forge = self._forge(repo, FACTORY_REMOTE, _DeclaresABot())
        glab = _Glab()

        with patch("teatree.core.push.fast_push.run_checked", glab):
            assert forge.create_pr(branch="b", title="t", body="d") == MR_URL

        assert glab.tokens_that("glab", "mr", "create") == [BOT_TOKEN]

    def test_the_identity_read_back_runs_under_the_token_that_creates(self, repo: Path) -> None:
        forge = self._forge(repo, FACTORY_REMOTE, _DeclaresABot())
        glab = _Glab()

        with patch("teatree.core.push.fast_push.run_checked", glab):
            forge.create_pr(branch="b", title="t", body="d")

        assert glab.tokens_that("glab", "api", "user") == glab.tokens_that("glab", "mr", "create") == [BOT_TOKEN]

    def test_an_unreachable_bot_refuses_by_name_and_creates_nothing(self, repo: Path) -> None:
        forge = self._forge(repo, FACTORY_REMOTE, _DeclaresABot(bot=""))
        glab = _Glab()

        with patch("teatree.core.push.fast_push.run_checked", glab), pytest.raises(UnapprovableAuthorError) as refusal:
            forge.create_pr(branch="b", title="t", body="d")

        assert FACTORY_REMOTE in str(refusal.value)
        assert glab.calls == []

    def test_a_bot_slot_that_authenticates_as_an_approver_is_refused_before_the_create(self, repo: Path) -> None:
        forge = self._forge(repo, FACTORY_REMOTE, _DeclaresABot())
        glab = _Glab({BOT_TOKEN: "the-owner"})

        with patch("teatree.core.push.fast_push.run_checked", glab), pytest.raises(UnapprovableAuthorError) as refusal:
            forge.create_pr(branch="b", title="t", body="d")

        assert "the-owner" in str(refusal.value)
        assert glab.tokens_that("glab", "mr", "create") == []

    def test_an_ordinary_repo_keeps_the_owner_token_and_is_never_read_back(self, repo: Path) -> None:
        forge = self._forge(repo, PLAIN_REMOTE, _DeclaresABot(bot=""))
        glab = _Glab()

        with patch("teatree.core.push.fast_push.run_checked", glab):
            assert forge.create_pr(branch="b", title="t", body="d") == MR_URL

        assert glab.tokens_that("glab", "mr", "create") == [OWNER_TOKEN]
        assert glab.tokens_that("glab", "api", "user") == []

    def test_an_update_never_needs_the_bot(self, repo: Path) -> None:
        forge = self._forge(repo, FACTORY_REMOTE, _DeclaresABot(bot=""))
        glab = _Glab()

        with patch("teatree.core.push.fast_push.run_checked", glab):
            forge.update_pr(url=MR_URL, body="d2")

        assert glab.calls == [(["glab", "mr", "update", "9", "--description", "d2"], OWNER_TOKEN)]

    def test_a_whole_fast_push_pushes_and_opens_no_mr_when_the_bot_is_unreachable(
        self, repo: Path, leak_env: None
    ) -> None:
        forge = self._forge(repo, FACTORY_REMOTE, _DeclaresABot(bot=""))
        glab = _Glab()

        outcome = self._whole_fast_push(repo, forge, glab)

        assert outcome.pushed
        assert outcome.pr_action == UNAPPROVABLE_AUTHOR_PR_REFUSAL
        assert FACTORY_REMOTE in outcome.pr_skip_reason
        assert glab.calls == []

    def test_conflicting_declarations_are_a_named_outcome_not_a_traceback_after_the_push(
        self, repo: Path, leak_env: None
    ) -> None:
        forge = self._forge(repo, FACTORY_REMOTE, _DeclaresABot(), _DeclaresABot(bot="other-bot-token"))
        glab = _Glab()

        outcome = self._whole_fast_push(repo, forge, glab)

        assert outcome.pushed
        assert outcome.pr_action == UNAPPROVABLE_AUTHOR_PR_REFUSAL
        assert "DIFFERENT" in outcome.pr_skip_reason
        assert glab.calls == []


class TestAMergeRequestRefusedOnItsAuthorKeepsThePushLanded:
    """Like the empty-delta skip, a withheld MR must never strand the work it was checkpointing."""

    def _refused(self, repo: Path, leak_env: None) -> FastPushOutcome:
        class RefusingForge(FakeForge):
            def create_pr(self, *, branch: str, title: str, body: str) -> str:
                msg = f"{FACTORY_REMOTE} would be authored by the owner"
                raise UnapprovableAuthorError(msg)

        (repo / "feature.py").write_text("x = 1\n")
        return run_fast_push(repo, RefusingForge(), message="feat: clean change")

    def test_the_outcome_names_the_refusal_and_carries_no_url(self, repo: Path, leak_env: None) -> None:
        outcome = self._refused(repo, leak_env)

        assert outcome.pr_action == UNAPPROVABLE_AUTHOR_PR_REFUSAL
        assert FACTORY_REMOTE in outcome.pr_skip_reason
        assert outcome.pr_url == ""

    def test_the_push_still_lands(self, repo: Path, leak_env: None) -> None:
        outcome = self._refused(repo, leak_env)

        assert outcome.ok
        assert outcome.pushed
        assert (
            "refs/heads/feature" in run_checked(["git", "ls-remote", "--heads", "origin", "feature"], cwd=repo).stdout
        )

    def test_an_existing_mr_is_still_updated(self, repo: Path, leak_env: None) -> None:
        forge = FakeForge(existing_pr_url=MR_URL)
        (repo / "feature.py").write_text("x = 1\n")

        outcome = run_fast_push(repo, forge, message="feat: clean change")

        assert outcome.pr_action == "updated"
        assert forge.updated[0]["url"] == MR_URL
