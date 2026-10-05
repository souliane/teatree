"""Host-qualified private declarations stay bound to their actual forge."""

import json
import shlex
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from teatree.config.write_validation import validate_config_write
from teatree.core.gates.privacy_gate import _target_is_public
from teatree.core.review.author_trust import repo_is_internal
from teatree.hooks import (
    _private_repo_entries,
    _repo_visibility,
    _ssh_alias,
    own_repo_url_carve_out,
    public_visibility,
    publish_destination,
    publish_surface,
)
from teatree.hooks.leak_policy import Visibility
from teatree.hooks.repo_visibility_cli import _resolve
from teatree.utils.run import TimeoutExpired


def _config(path: Path, entries: list[str]) -> Path:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'private_repos', ?)",
            (json.dumps(entries),),
        )
    return path


@pytest.mark.parametrize(
    "entry",
    [
        "acme-eng",
        "acme-eng/widget",
        "localhost/acme-eng",
        "gitlab.com",
        "gitlab.com/*",
        "gitlab.com/acme?",
        "gitlab.com/a b",
    ],
)
def test_write_rejects_invalid_private_repo_entry(entry: str) -> None:
    with pytest.raises(ValueError, match="host/owner"):
        validate_config_write("private_repos", [entry])


def test_write_normalizes_git_suffix() -> None:
    assert validate_config_write("private_repos", ["GitLab.Com/Acme-Eng/Widget.git"]) == ["gitlab.com/acme-eng/widget"]
    assert validate_config_write("private_repos", ["GitLab.Com/Acme-Eng.git"]) == ["gitlab.com/acme-eng"]
    assert validate_config_write("private_repos", ["GitLab.Com/Acme-Eng/Widget.GIT/"]) == ["gitlab.com/acme-eng/widget"]


@pytest.mark.parametrize("probe", ["PUBLIC", "PRIVATE", None])
def test_probe_precedence_is_shared_across_consumers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe: str | None
) -> None:
    db = _config(tmp_path / "config.sqlite3", ["gitlab.com/acme-eng"])
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.setattr(_repo_visibility, "slug_visibility", lambda _slug: probe)
    monkeypatch.setattr("teatree.hooks.repo_visibility_cli.slug_visibility", lambda _slug: probe)
    monkeypatch.setattr(own_repo_url_carve_out, "slug_visibility", lambda _slug: probe)
    slug = "gitlab.com/acme-eng/widget"
    private = probe != "PUBLIC"
    assert _private_repo_entries.private_repo_visibility(
        slug,
        db,
        ops=_repo_visibility,
    ) == ("PUBLIC" if not private else probe or "PRIVATE")
    assert (
        publish_surface.segment_target_is_private(
            ["glab", "issue", "create", "--repo", "acme-eng/widget"], None, config_path=db
        )
        is private
    )
    dest = publish_destination.Destination(slug="acme-eng/widget", via="flag", forge="gitlab")
    assert publish_destination.is_public_destination(dest, config_path=db) is (not private)
    assert public_visibility.destination_visibility(dest, config_path=db) == (
        Visibility.NON_PUBLIC if private else Visibility.PUBLIC
    )
    assert _resolve("git@gitlab.com:acme-eng/widget.git").verdict == ("PRIVATE" if private else "PUBLIC")
    assert (
        own_repo_url_carve_out._url_is_own_repo(
            "https://gitlab.com/acme-eng/widget/-/issues/1", ["gitlab.com/acme-eng"]
        )
        is private
    )


def test_same_owner_on_github_never_matches_gitlab_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _config(tmp_path / "config.sqlite3", ["gitlab.com/acme-eng"])
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.setattr(_repo_visibility, "slug_visibility", lambda _slug: None)
    monkeypatch.setattr("teatree.hooks.repo_visibility_cli.slug_visibility", lambda _slug: None)
    slug = "github.com/acme-eng/oss-lib"
    assert not _repo_visibility.slug_is_allowlisted_private(slug, db)
    assert not publish_surface.segment_target_is_private(
        ["gh", "issue", "create", "--repo", "acme-eng/oss-lib"], None, config_path=db
    )
    dest = publish_destination.Destination(slug="acme-eng/oss-lib", via="flag", forge="github")
    assert publish_destination.is_public_destination(dest, config_path=db)
    assert public_visibility.destination_visibility(dest, config_path=db) == Visibility.UNKNOWN
    assert _resolve("https://github.com/acme-eng/oss-lib.git").verdict == "UNKNOWN"


@pytest.mark.parametrize(
    "command",
    [
        "gh issue create --repo souliane/teatree --body x",
        "gh api repos/souliane/teatree/issues -f body=x",
    ],
)
def test_github_post_forms_share_public_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    db = _config(tmp_path / "config.sqlite3", ["github.com/souliane"])
    monkeypatch.setattr(_repo_visibility, "slug_visibility", lambda _slug: "PUBLIC")
    dest = publish_destination.resolve_publish_destination(command)
    assert dest is not None
    assert publish_destination.is_public_destination(dest, config_path=db)
    assert public_visibility.destination_visibility(dest, config_path=db) == Visibility.PUBLIC


def test_ssh_alias_uses_offline_hostname_and_unresolved_alias_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("ssh") is None:
        pytest.skip("OpenSSH is unavailable in this runner")
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "config").write_text("Host gh-acct\n  HostName github.com\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("teatree.hooks.repo_visibility_cli.slug_visibility", lambda _slug: None)
    assert _repo_visibility.slug_for_remote_url("git@gh-acct:souliane/teatree.git") == "github.com/souliane/teatree"
    assert _repo_visibility.slug_for_remote_url("git@missing:souliane/teatree.git") == ""
    assert _resolve("git@missing:souliane/teatree.git").verdict == "UNKNOWN"


def test_ssh_alias_reads_system_config_and_caches_per_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="hostname github.com\n")

    monkeypatch.setattr(_ssh_alias.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_ssh_alias, "run_bounded_group", fake_run)
    assert _repo_visibility.slug_for_remote_url("git@-alias:acme/widget.git") == "github.com/acme/widget"
    assert _repo_visibility.slug_for_remote_url("git@-alias:acme/widget.git") == "github.com/acme/widget"
    assert len(calls) == 1
    assert calls[0][-3:] == ["-G", "--", "-alias"]


def test_localhost_stays_a_host() -> None:
    assert _repo_visibility.slug_for_remote_url("ssh://git@localhost:2222/acme/widget.git") == "localhost/acme/widget"
    assert _repo_visibility.slug_for_remote_url("git@localhost:acme/widget.git") == "localhost/acme/widget"


def test_ssh_alias_timeout_stays_unknown_within_the_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def timed_out(_argv: list[str], **kwargs: object) -> None:
        calls.append(kwargs["timeout"])
        raise TimeoutExpired(["ssh", "-G"], 2)

    monkeypatch.setattr(_ssh_alias.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_ssh_alias, "run_bounded_group", timed_out)
    assert _repo_visibility.slug_for_remote_url("git@timed-alias:acme/widget.git") == ""
    assert _resolve("git@timed-alias:acme/widget.git").verdict == "UNKNOWN"
    assert calls == [2, 2]


def test_ssh_alias_retries_after_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def retry(_argv: list[str], **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutExpired(["ssh", "-G"], 2)
        return SimpleNamespace(returncode=0, stdout="hostname gitlab.com\n")

    monkeypatch.setattr(_ssh_alias.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_ssh_alias, "run_bounded_group", retry)
    assert _repo_visibility.slug_for_remote_url("git@retry-alias:acme/widget.git") == ""
    assert _repo_visibility.slug_for_remote_url("git@retry-alias:acme/widget.git") == "gitlab.com/acme/widget"
    assert calls == 2


def test_missing_ssh_stays_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_ssh(_argv: list[str], **_kwargs: object) -> None:
        command = "ssh"
        raise FileNotFoundError(command)

    monkeypatch.setattr(_ssh_alias.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(_ssh_alias.shutil, "which", lambda _tool: None)
    monkeypatch.setattr(_ssh_alias, "run_bounded_group", missing_ssh)
    assert _repo_visibility.slug_for_remote_url("git@absent-alias:acme/widget.git") == ""
    assert _resolve("git@absent-alias:acme/widget.git").verdict == "UNKNOWN"


def test_malformed_stored_entry_is_ignored_with_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _config(tmp_path / "config.sqlite3", ["acme-eng"])
    assert not _repo_visibility.slug_is_allowlisted_private("github.com/acme-eng/oss-lib", db)
    assert "expected host/owner" in capsys.readouterr().err


def test_github_url_flag_api_and_env_share_public_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _config(tmp_path / "config.sqlite3", ["github.com/souliane"])
    monkeypatch.setattr(_repo_visibility, "slug_visibility", lambda _slug: "PUBLIC")
    commands = [
        "gh issue comment https://github.com/souliane/teatree/issues/2 --body x",
        "gh api repos/souliane/teatree/issues -f body=x",
        "gh issue create --repo souliane/teatree --body x",
        "gh issue create --body x",
    ]
    monkeypatch.setenv("GH_REPO", "souliane/teatree")
    for command in commands:
        dest = publish_destination.resolve_publish_destination(command)
        assert dest is not None
        assert publish_destination.is_public_destination(dest, config_path=db)
        assert public_visibility.destination_visibility(dest, config_path=db) == Visibility.PUBLIC


@pytest.mark.parametrize(
    "command",
    [
        "gh issue comment https://github.com/acme/widget/issues/2 --body x",
        "gh api repos/acme/widget/issues -f body=x",
        "gh issue create --repo acme/widget --body x",
        "gh issue create --body x",
    ],
)
@pytest.mark.parametrize("probe", ["PUBLIC", None])
def test_all_github_destination_forms_agree_across_visibility_consumers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, probe: str | None
) -> None:
    db = _config(tmp_path / "config.sqlite3", ["github.com/acme"])
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.setenv("GH_REPO", "acme/widget")
    monkeypatch.setattr(_repo_visibility, "slug_visibility", lambda _slug: probe)
    monkeypatch.setattr(own_repo_url_carve_out, "slug_visibility", lambda _slug: probe)
    dest = publish_destination.resolve_publish_destination(command)
    assert dest is not None
    private = probe is None
    assert publish_destination.is_public_destination(dest, config_path=db) is (not private)
    assert public_visibility.destination_visibility(dest, config_path=db) == (
        Visibility.NON_PUBLIC if private else Visibility.PUBLIC
    )
    assert _target_is_public(dest.slug, dest.forge) is (not private)
    assert publish_surface.segment_target_is_private(shlex.split(command), None, config_path=db) is private
    assert repo_is_internal("acme/widget", pr_url="https://github.com/acme/widget/pull/2") is private
    assert (
        own_repo_url_carve_out._url_is_own_repo("https://github.com/acme/widget/issues/2", ["github.com/acme"])
        is private
    )
    assert repo_is_internal("acme-other/widget", pr_url="https://github.com/acme-other/widget/pull/2") is False
    assert (
        own_repo_url_carve_out._url_is_own_repo("https://github.com/acme-other/widget/issues/2", ["github.com/acme"])
        is False
    )


def test_author_uses_actual_pr_url_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _config(tmp_path / "config.sqlite3", ["forge.example.test/acme"])
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.setattr(_repo_visibility, "slug_visibility", lambda _slug: None)
    assert repo_is_internal("acme/widget", pr_url="https://forge.example.test/acme/widget/-/merge_requests/2")


def test_author_and_outbound_gate_keep_gitlab_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _config(tmp_path / "config.sqlite3", ["gitlab.com/acme-eng"])
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.setattr(_repo_visibility, "slug_visibility", lambda _slug: None)
    assert repo_is_internal("acme-eng/widget", pr_url="https://gitlab.com/acme-eng/widget/-/merge_requests/2")
    assert not repo_is_internal("acme-other/widget", pr_url="https://gitlab.com/acme-other/widget/-/merge_requests/1")
    assert not repo_is_internal("acme-eng/oss-lib", pr_url="https://github.com/acme-eng/oss-lib/pull/1")
    assert not _target_is_public("acme-eng/widget", "gitlab")
    assert _target_is_public("acme-eng/oss-lib", "github")


def test_public_probe_overrides_owner_group_for_push_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _config(tmp_path / "config.sqlite3", ["github.com/souliane"])
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.setattr("teatree.hooks.repo_visibility_cli.slug_visibility", lambda _slug: "PUBLIC")
    assert _resolve("https://github.com/souliane/teatree.git").verdict == "PUBLIC"


def test_host_tokens_are_not_own_repo_terms(tmp_path: Path) -> None:
    db = _config(tmp_path / "config.sqlite3", ["gitlab.com/acme-eng/widget"])
    assert not _repo_visibility.term_is_own_repo_slug("gitlab", db)
    assert not _repo_visibility.term_is_own_repo_slug("com", db)
    assert _repo_visibility.term_is_own_repo_slug("acme", db)


def test_commit_remote_uses_real_host_for_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _config(tmp_path / "config.sqlite3", ["gitlab.com/acme-eng"])
    monkeypatch.setattr(_repo_visibility, "slug_visibility", lambda _slug: None)
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/acme-eng/oss-lib.git\n')
    assert not publish_surface.commit_targets_private_repo(clone, config_path=db)
    (clone / ".git" / "config").write_text('[remote "origin"]\n\turl = git@gitlab.com:acme-eng/widget.git\n')
    assert publish_surface.commit_targets_private_repo(clone, config_path=db)
