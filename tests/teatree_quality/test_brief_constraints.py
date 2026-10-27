"""Plain-word plan limits are checked against the actual diff."""

from pathlib import Path

from teatree.quality.brief_constraints import check_constraints, extract_constraints
from teatree.quality.changed_set import ChangedSet, ChangeEntry


def test_reads_combined_negative_and_named_file_constraints() -> None:
    parsed = extract_constraints(
        "Do not add a migration or a setting. No new public name. Keep src/stable.py unchanged."
    )
    assert {(item.kind, item.target) for item in parsed} == {
        ("no_migration", ""),
        ("no_setting", ""),
        ("no_public_name", ""),
        ("unchanged_file", "src/stable.py"),
    }


def test_describing_the_constraint_grammar_does_not_activate_its_examples() -> None:
    description = (
        "PR5: constraints. quality/brief_constraints.py, closed grammar "
        "(no migration, no new setting, no new public name, a named file unchanged)."
    )
    assert extract_constraints(description) == ()
    assert {item.kind for item in extract_constraints(description + "\nDo not add a migration.")} == {"no_migration"}
    assert {item.kind for item in extract_constraints("Constraints: (no migration)")} == {"no_migration"}


def test_do_not_forget_is_not_a_negative_setting_constraint() -> None:
    assert extract_constraints("Do not forget to update the settings docs.") == ()


def test_quoted_external_issue_limit_is_not_an_instruction() -> None:
    text = (
        'Issue #42 says "No new setting or flag"; this plan permits a setting.\n'
        "> No migration.\n"
        "Another pull request states No new public name.\n"
    )
    assert extract_constraints(text) == ()


def test_own_instruction_after_a_quote_still_applies() -> None:
    assert {item.kind for item in extract_constraints('Issue #42 says "No new setting." Do not add a migration.')} == {
        "no_migration"
    }


def test_own_issue_and_pr_constraints_are_not_discarded_as_attribution() -> None:
    assert {item.kind for item in extract_constraints("The issue requires no migration.")} == {"no_migration"}
    assert {item.kind for item in extract_constraints("Issue #5452 requires: no new setting.")} == {"no_setting"}
    assert {
        (item.kind, item.target) for item in extract_constraints("This PR requires that foo.py stays unchanged.")
    } == {("unchanged_file", "foo.py")}


def test_quoted_targets_and_own_issue_instruction_remain_constraints() -> None:
    assert {(item.kind, item.target) for item in extract_constraints('Do not touch "src/foo.py".')} == {
        ("unchanged_file", "src/foo.py")
    }
    assert {(item.kind, item.target) for item in extract_constraints('Keep "settings.py" unchanged.')} == {
        ("unchanged_file", "settings.py")
    }
    assert {item.kind for item in extract_constraints('Do not add a "new" setting.')} == {"no_setting"}
    assert {item.kind for item in extract_constraints("Issue #5452 asks that you do not add a migration.")} == {
        "no_migration"
    }
    assert {
        item.kind
        for item in extract_constraints("Issue #5452 asks that you do not add a migration.", own_issue_number="5452")
    } == {"no_migration"}
    assert extract_constraints("Issue #42 asks that you do not add a migration.", own_issue_number="5452") == ()


def test_cli_settings_flag_is_not_a_ban_on_new_settings() -> None:
    brief = 'Use the advisor key without spawning: `--settings {"advisorModel": "<id>"}`.'

    assert extract_constraints(brief) == ()
    assert {item.kind for item in extract_constraints("Do not add a setting.")} == {"no_setting"}


def test_hyphenated_setting_names_remain_constraints() -> None:
    for name in ("user-setting", "per-overlay-settings", "box-global-setting"):
        assert {item.kind for item in extract_constraints(f"Do not add a {name}.")} == {"no_setting"}


def test_settings_flag_prefix_keeps_a_hyphenated_name_as_a_constraint() -> None:
    assert {item.kind for item in extract_constraints("Do not add --settings-file.")} == {"no_setting"}


def test_settings_flag_suffix_keeps_a_prefixed_name_as_a_constraint() -> None:
    assert {item.kind for item in extract_constraints("Do not add foo--settings.")} == {"no_setting"}


def test_an_import_is_not_a_new_public_definition() -> None:
    path = "src/teatree/core/example.py"
    changes = ChangedSet((ChangeEntry("M", path),), "origin/main")
    violations = check_constraints(
        extract_constraints("No new public name."),
        changes,
        before={path: "def existing() -> None: pass\n"},
        after={path: "import collections\nfrom pathlib import Path\ndef existing() -> None: pass\n"},
    )
    assert violations == ()


def test_blocks_migration_and_named_file_change(tmp_path: Path) -> None:
    changes = ChangedSet(
        (
            ChangeEntry("A", "src/teatree/core/migrations/0002_new.py"),
            ChangeEntry("M", "src/stable.py"),
        ),
        "origin/main",
    )
    constraints = extract_constraints("No migration. Keep src/stable.py unchanged.")
    violations = check_constraints(constraints, changes, before={}, after={})
    assert {item.kind for item in violations} == {"no_migration", "unchanged_file"}


def test_blocks_added_public_name_and_setting_field() -> None:
    changes = ChangedSet((ChangeEntry("M", "src/teatree/config/settings.py"),), "origin/main")
    before = {"src/teatree/config/settings.py": "class AppSettings:\n    old: int = 1\n"}
    after = {
        "src/teatree/config/settings.py": (
            "class AppSettings:\n    old: int = 1\n    new: int = 2\n\ndef public_api() -> None:\n    pass\n"
        )
    }
    violations = check_constraints(
        extract_constraints("No new setting or public name."), changes, before=before, after=after
    )
    assert {item.kind for item in violations} == {"no_setting", "no_public_name"}


def test_missing_source_is_a_failed_probe() -> None:
    changes = ChangedSet((ChangeEntry("M", "src/teatree/config/settings.py"),), "origin/main")
    violations = check_constraints(extract_constraints("No new setting."), changes, before={}, after={})
    assert len(violations) == 1
    assert "unavailable" in violations[0].detail


def test_no_setting_covers_registry_keys_and_no_public_name_covers_constants() -> None:
    path = "src/teatree/config/registries.py"
    changes = ChangedSet((ChangeEntry("M", path),), "origin/main")
    before = {path: "COLD_SETTINGS = {'old': int}\n"}
    after = {path: "COLD_SETTINGS = {'old': int, 'new': str}\nPUBLIC_TOKEN = 1\n"}
    violations = check_constraints(
        extract_constraints("No new setting or public name."), changes, before=before, after=after
    )
    assert {item.kind for item in violations} == {"no_setting", "no_public_name"}
