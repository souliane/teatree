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


def test_no_setting_does_not_count_declaration_metadata_as_a_runtime_setting() -> None:
    path = "src/teatree/config/declaration.py"
    changes = ChangedSet((ChangeEntry("M", path),), "origin/main")
    before = {path: "class SettingSpec:\n    help: str = ''\n"}
    after = {path: "class SettingSpec:\n    help: str = ''\n    bounds: object = None\n"}

    assert check_constraints(extract_constraints("No new setting."), changes, before=before, after=after) == ()


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


def test_named_setting_exception_preserves_the_other_setting_blocks() -> None:
    path = "src/teatree/config/settings.py"
    changes = ChangedSet((ChangeEntry("M", path),), "origin/main")
    before = {path: "class AppSettings:\n    old: int = 1\n"}
    brief = "No new setting.\nAllowed setting additions: AppSettings.approved"
    approved = {path: "class AppSettings:\n    old: int = 1\n    approved: int = 2\n"}
    extra = {path: approved[path] + "    unapproved: int = 3\n"}

    assert check_constraints(extract_constraints(brief), changes, before=before, after=approved) == ()
    violations = check_constraints(extract_constraints(brief), changes, before=before, after=extra)
    assert len(violations) == 1
    assert violations[0].kind == "no_setting"
    assert "AppSettings.unapproved" in violations[0].detail
