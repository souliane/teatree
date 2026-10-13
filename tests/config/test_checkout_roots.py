import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.config.checkout_roots import checkout_roots
from teatree.core.models import ConfigSetting


def test_allowed_checkout_roots_resolve_symlinks_and_include_stored_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    clone = home / "workspace" / "repo"
    durable = tmp_path / "durable"
    durable.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(durable)
    roots = checkout_roots(home=home, clone_dir=clone, stored=(str(durable),), disposable=())

    assert roots.allows(home / "workspace" / "t3-workspaces", durable=True)
    assert roots.allows(alias / "ticket" / "repo", durable=True)
    assert not roots.allows(tmp_path / "elsewhere", durable=True)


def test_unsafe_stored_roots_are_dropped_and_named(tmp_path: Path) -> None:
    home = tmp_path / "home"
    clone = home / "workspace" / "repo"
    temp = tmp_path / "temp"
    roots = checkout_roots(
        home=home,
        clone_dir=clone,
        stored=(str(home), str(clone), str(tmp_path), str(temp / "inside")),
        disposable=(temp,),
    )

    assert len(roots.dropped) == 4
    assert all(name in " ".join(roots.dropped) for name in (str(home), str(clone), str(tmp_path), str(temp / "inside")))
    assert not roots.allows(home / "notes", durable=True)


def test_disposable_and_harness_roots_are_allowed_for_matching_context(tmp_path: Path) -> None:
    home = tmp_path / "home"
    clone = home / "workspace" / "repo"
    temp = tmp_path / "temp"
    roots = checkout_roots(home=home, clone_dir=clone, source_clone=clone, stored=(), disposable=(temp,))

    assert roots.allows(temp / "review")
    assert not roots.allows(temp / "review", durable=True)
    assert roots.allows(clone / ".claude" / "worktrees" / "agent")
    assert not roots.allows(clone / ".claude" / "worktrees" / "agent", durable=True)


def test_unreadable_store_falls_back_to_base_and_reports_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with patch("teatree.config.checkout_roots._stored_workspace_dirs", return_value=((), "locked")):
        roots = checkout_roots(home=home, clone_dir=home / "workspace" / "repo")

    assert roots.allows(home / "workspace" / "t3-workspaces" / "ticket", durable=True)
    assert "locked" in roots.refusal(tmp_path / "outside", durable=True)
    assert "workspace_dir" in roots.refusal(tmp_path / "outside", durable=True)


def test_all_stored_scopes_are_allowed_when_the_store_is_readable(tmp_path: Path) -> None:
    db = tmp_path / "settings.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE teatree_config_setting (scope TEXT, key TEXT, value TEXT)")
        for scope, root in (("", tmp_path / "global"), ("other", tmp_path / "other")):
            conn.execute(
                "INSERT INTO teatree_config_setting VALUES (?, ?, ?)",
                (scope, "workspace_dir", json.dumps(str(root))),
            )
    with patch("teatree.config.checkout_roots.cold_db.canonical_config_db", return_value=db):
        roots = checkout_roots(home=tmp_path / "home", clone_dir=tmp_path / "clones", disposable=())

    assert roots.allows(tmp_path / "global" / "ticket", durable=True)
    assert roots.allows(tmp_path / "other" / "ticket", durable=True)
    assert roots.read_error == ""


class TestActiveSettingsStore(TestCase):
    def test_reads_all_scopes_from_the_active_django_database(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "/srv/global-checkouts", scope="")
        ConfigSetting.objects.set_value("workspace_dir", "/srv/other-checkouts", scope="other")

        roots = checkout_roots(home=Path("/fixture-home"), clone_dir=Path("/fixture-home/workspace"), disposable=())

        assert roots.allows(Path("/srv/global-checkouts/ticket"), durable=True)
        assert roots.allows(Path("/srv/other-checkouts/ticket"), durable=True)
