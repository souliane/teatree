"""The host-routed foreign-open-MR resolver behind the pre-push guard.

The guard resolved the backing MR with ``gh`` alone and gave up when ``gh`` was
absent, so on a GitLab remote it could never fire: the one forge it knew how to
ask was the one the branch does not live on, and a guard that cannot fire is
indistinguishable from one that found nothing.

Integration in the spirit of the Test-Writing Doctrine: real forge CLIs are
faked as executables on ``PATH`` (the unstoppable network), everything else runs
for real. Each shim answers in its tool's OWN idiom — ``gh`` honours ``--jq``,
``glab api`` has no such flag and returns JSON.
"""

import json
import os
import sqlite3
import stat
from io import StringIO
from pathlib import Path
from typing import ClassVar

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config.secret_settings import PERSONAL_IDENTIFIERS
from teatree.core.models.config_setting import ConfigSetting
from teatree.hooks.foreign_mr_cli import NONE_VERDICT, foreign_mr_verdict

_GITLAB_REMOTE = "https://gitlab.com/acme-eng/widget.git"
_GITHUB_REMOTE = "https://github.com/acme/widget.git"


def _write_shim(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / name
    shim.write_text(f"#!/usr/bin/env python3\nimport json, sys\nargs = sys.argv[1:]\n{body}\nsys.exit(1)\n", "utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _glab_shim(bin_dir: Path, *, username: str, merge_requests: list[dict[str, object]]) -> None:
    _write_shim(
        bin_dir,
        "glab",
        "if args[:2] == ['api', 'user']:\n"
        f"    print(json.dumps({{'username': {username!r}}}))\n"
        "    sys.exit(0)\n"
        "if args[0] == 'api' and 'merge_requests' in args[1]:\n"
        f"    rows = json.loads({json.dumps(merge_requests)!r})\n"
        "    branch = args[1].split('source_branch=')[1].split('&')[0]\n"
        "    print(json.dumps([r for r in rows if r['source_branch'] == branch]))\n"
        "    sys.exit(0)\n",
    )


def _gh_shim(bin_dir: Path, *, login: str, prs: list[dict[str, object]]) -> None:
    _write_shim(
        bin_dir,
        "gh",
        "if args[:2] == ['api', 'user']:\n"
        f"    print({login!r})\n"
        "    sys.exit(0)\n"
        "if args[:2] == ['pr', 'list']:\n"
        f"    rows = json.loads({json.dumps(prs)!r})\n"
        "    head = args[args.index('--head') + 1]\n"
        "    for pr in rows:\n"
        "        if pr['headRefName'] == head:\n"
        "            print(f\"{pr['number']}\\t{pr['author']['login']}\")\n"
        "    sys.exit(0)\n",
    )


@pytest.fixture
def forge_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty dir prepended to PATH, so only the shims a test writes resolve."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


def _mr(branch: str, author: str, iid: int = 77) -> dict[str, object]:
    return {"iid": iid, "source_branch": branch, "author": {"username": author}}


class TestGitLabRemotesAreResolved:
    def test_a_foreign_open_mr_on_a_gitlab_remote_is_reported(self, forge_bin: Path) -> None:
        _glab_shim(forge_bin, username="me", merge_requests=[_mr("feature-x", "teammate")])
        assert foreign_mr_verdict(_GITLAB_REMOTE, "feature-x") == "FOREIGN 77 teammate me"

    def test_our_own_open_mr_is_reported_as_ours(self, forge_bin: Path) -> None:
        _glab_shim(forge_bin, username="me", merge_requests=[_mr("feature-x", "Me")])
        assert foreign_mr_verdict(_GITLAB_REMOTE, "feature-x") == "OWN 77"

    def test_a_branch_with_no_open_mr_is_none(self, forge_bin: Path) -> None:
        _glab_shim(forge_bin, username="me", merge_requests=[_mr("other-branch", "teammate")])
        assert foreign_mr_verdict(_GITLAB_REMOTE, "feature-x") == NONE_VERDICT

    def test_a_gitlab_remote_is_never_routed_to_gh(self, forge_bin: Path) -> None:
        # Only `gh` is installed. Routing a GitLab remote to it is exactly the
        # defect: it would answer about a repo on the wrong forge, or not at all.
        _gh_shim(forge_bin, login="me", prs=[{"number": 5, "headRefName": "feature-x", "author": {"login": "them"}}])
        assert foreign_mr_verdict(_GITLAB_REMOTE, "feature-x") == NONE_VERDICT


class TestGitHubRemotesKeepWorking:
    def test_a_foreign_open_pr_on_a_github_remote_is_reported(self, forge_bin: Path) -> None:
        _gh_shim(forge_bin, login="me", prs=[{"number": 42, "headRefName": "feature-x", "author": {"login": "them"}}])
        assert foreign_mr_verdict(_GITHUB_REMOTE, "feature-x") == "FOREIGN 42 them me"


class TestEveryUnresolvableStepFailsOpen:
    @pytest.mark.parametrize(
        ("remote", "branch"),
        [
            ("", "feature-x"),  # no remote to normalise
            ("https://example.invalid/acme/widget.git", "feature-x"),  # host routes nowhere
            (_GITLAB_REMOTE, ""),  # no branch to ask about
        ],
    )
    def test_an_unaskable_question_is_none(self, remote: str, branch: str, forge_bin: Path) -> None:
        _glab_shim(forge_bin, username="me", merge_requests=[_mr("feature-x", "teammate")])
        assert foreign_mr_verdict(remote, branch) == NONE_VERDICT

    def test_no_forge_cli_on_path_is_none(self, forge_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", str(forge_bin))
        assert foreign_mr_verdict(_GITLAB_REMOTE, "feature-x") == NONE_VERDICT

    def test_an_unresolvable_login_is_none(self, forge_bin: Path) -> None:
        # The MR resolves but the identity does not, so "foreign" cannot be
        # established — reporting it would block on a guess.
        _write_shim(
            forge_bin,
            "glab",
            "if args[0] == 'api' and 'merge_requests' in args[1]:\n"
            f"    print(json.dumps([{json.dumps(_mr('feature-x', 'teammate'))}]))\n"
            "    sys.exit(0)\n",
        )
        assert foreign_mr_verdict(_GITLAB_REMOTE, "feature-x") == NONE_VERDICT


def _seed_self_identities(db: Path, identities: dict[str, object]) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
        "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'self_forge_identities', ?)",
        (json.dumps(identities),),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def config_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The cold-read config DB the guard resolves its own identities from."""
    db = tmp_path / "config.sqlite3"
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return db


class TestOurOwnBotIsNotATeammate:
    """A factory bot authors our MRs so we stay eligible to approve them.

    The guard asks ONE forge CLI who we are, so a bot-authored MR of our own
    reads exactly like a teammate's and every push to it is refused. The
    operator declares the identities it also acts as, per host; nothing else
    becomes ours.
    """

    def test_an_mr_authored_by_a_declared_self_identity_is_ours(self, forge_bin: Path, config_db: Path) -> None:
        _seed_self_identities(config_db, {"gitlab.com": ["acme-factory-bot"]})
        _glab_shim(forge_bin, username="me", merge_requests=[_mr("feature-x", "acme-factory-bot")])
        assert foreign_mr_verdict(_GITLAB_REMOTE, "feature-x") == "OWN 77"

    def test_a_teammate_is_still_foreign_while_a_self_identity_is_declared(
        self, forge_bin: Path, config_db: Path
    ) -> None:
        # The load-bearing half: declaring our bot must not make everyone ours.
        _seed_self_identities(config_db, {"gitlab.com": ["acme-factory-bot"]})
        _glab_shim(forge_bin, username="me", merge_requests=[_mr("feature-x", "teammate")])
        assert foreign_mr_verdict(_GITLAB_REMOTE, "feature-x") == "FOREIGN 77 teammate me"

    def test_an_identity_declared_for_another_host_does_not_apply(self, forge_bin: Path, config_db: Path) -> None:
        _seed_self_identities(config_db, {"github.com": ["acme-factory-bot"]})
        _glab_shim(forge_bin, username="me", merge_requests=[_mr("feature-x", "acme-factory-bot")])
        assert foreign_mr_verdict(_GITLAB_REMOTE, "feature-x") == "FOREIGN 77 acme-factory-bot me"

    def test_declaring_nothing_leaves_the_verdict_unchanged(self, forge_bin: Path, config_db: Path) -> None:
        _glab_shim(forge_bin, username="me", merge_requests=[_mr("feature-x", "acme-factory-bot")])
        assert foreign_mr_verdict(_GITLAB_REMOTE, "feature-x") == "FOREIGN 77 acme-factory-bot me"


class TestTheDocumentedEnablementPathWorks(TestCase):
    """``config_setting set self_forge_identities`` is the ONLY sanctioned way to declare one.

    The command refuses any key outside the known-key set, so an unregistered
    setting leaves a raw sqlite INSERT as the only way in — which never
    republishes the host projection the pre-push hook reads, and renders the row
    as ``[unknown — not a declared setting]`` beside a destructive clear remedy.
    A feature reachable only that way is not reachable.
    """

    SETTING = "self_forge_identities"
    VALUE: ClassVar[dict[str, list[str]]] = {"gitlab.com": ["acme-factory-bot"]}

    def test_the_setting_is_accepted_and_round_trips(self) -> None:
        call_command("config_setting", "set", self.SETTING, json.dumps(self.VALUE), stdout=StringIO())

        stored = ConfigSetting.objects.get_effective(self.SETTING, scope="")

        assert stored == self.VALUE

    def test_a_non_table_value_is_refused_before_it_is_stored(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            call_command("config_setting", "set", self.SETTING, '["acme-factory-bot"]', stderr=StringIO())

        assert exc_info.value.code == 2
        assert ConfigSetting.objects.get_effective(self.SETTING, scope="") is None

    def test_the_operators_own_logins_never_reach_a_shared_export(self) -> None:
        # A host-keyed login list is exactly the personal-identifier class: no brand term
        # for the content scan to catch, and no credential suffix for the rule to match.
        assert self.SETTING in PERSONAL_IDENTIFIERS
