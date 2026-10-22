from dataclasses import replace

from django.template.loader import get_template
from django.urls import reverse

from teatree.config.cold_defaults import shipped_defaults_table
from teatree.core.setting_control import SettingControl
from teatree.core.settings.settings_compare import CompareFilters, CompareView
from teatree.core.settings.settings_compare_rows import WITHHELD_REASON, Cell, CompareRow
from teatree.core.settings.settings_compare_sync import SEED, SETTING, Disposition
from teatree.core.settings.settings_peers import PeerSnapshot


def test_a_withheld_peer_value_cannot_be_copied_to_this_instance() -> None:
    row = CompareRow(
        surface=SETTING,
        scope="",
        title="merge_wip",
        subtitle="global scope",
        cells=(
            Cell(label="local", present=False, known=True, local=True),
            Cell(label="peer", present=True, known=True, value={"__redacted__": "secret", "sha256": "0" * 64}),
        ),
        redacted=True,
    )

    assert row.has_write_target
    assert row.unwritable_reason == WITHHELD_REASON
    assert not row.writable
    assert row.readings[1].opaque
    assert row.readings[1].wire_value == ""


def test_a_manual_row_does_not_offer_use_here() -> None:
    row = CompareRow(
        surface=SETTING,
        scope="",
        title="merge_wip",
        subtitle="global scope",
        cells=(
            Cell(label="local", present=True, known=True, value=2, local=True),
            Cell(label="peer", present=True, known=True, value=3),
        ),
        syncable=False,
        control=SettingControl("merge_wip", shipped_defaults_table()),
    )
    instances = (
        PeerSnapshot(label="local", url="", note="", payload={"instance": {}}),
        PeerSnapshot(label="peer", url="http://127.0.0.1/peer", note="", payload={"instance": {}}),
    )
    view = CompareView(
        instances=instances, compared=instances, rows=(row,), total_rows=1, compared_rows=1, differing_rows=1
    )

    assert row.writable
    assert row.outcome.disposition is Disposition.MANUAL
    rendered = get_template("dash/settings_compare.html").render(
        {"comparison": view, "filters": CompareFilters(), "refusals": ()}
    )
    assert 'class="compare-use-here"' not in rendered


def test_apply_without_a_prior_local_row_reverts_by_removing_the_new_row() -> None:
    row = CompareRow(
        surface=SETTING,
        scope="",
        title="merge_wip",
        subtitle="global scope",
        cells=(
            Cell(label="local", present=False, known=True, local=True),
            Cell(label="peer", present=True, known=True, value=True),
        ),
        control=SettingControl("merge_wip", shipped_defaults_table()),
    )
    instances = (
        PeerSnapshot(label="local", url="", note="", payload={"instance": {}}),
        PeerSnapshot(label="peer", url="http://127.0.0.1/peer", note="", payload={"instance": {}}),
    )
    view = CompareView(
        instances=instances, compared=instances, rows=(row,), total_rows=1, compared_rows=1, differing_rows=1
    )
    rendered = get_template("dash/settings_compare.html").render(
        {"comparison": view, "filters": CompareFilters(), "refusals": ()}
    )
    assert row.readings[1].apply_url
    restore_url = reverse("dash:settings_restore", args=["merge_wip"])
    assert f'class="compare-revert" hx-post="{restore_url}"' in rendered


def test_a_clear_disposition_uses_the_restore_endpoint() -> None:
    row = CompareRow(
        surface=SETTING,
        scope="",
        title="merge_wip",
        subtitle="global scope",
        cells=(
            Cell(
                label="local",
                present=True,
                known=True,
                value=True,
                default_value=False,
                default_recorded=True,
                local=True,
            ),
            Cell(label="peer", present=True, known=True, value=False),
        ),
        equals_shipped_default=True,
        control=SettingControl("merge_wip", shipped_defaults_table()),
    )
    assert row.outcome.disposition is Disposition.CLEAR
    assert row.readings[1].apply_url == reverse("dash:settings_restore", args=["merge_wip"])


def test_each_peer_apply_uses_its_own_value_against_the_local_default() -> None:
    row = CompareRow(
        surface=SETTING,
        scope="",
        title="merge_wip",
        subtitle="global scope",
        cells=(
            Cell(label="local", present=True, known=True, value=2, default_value=0, default_recorded=True, local=True),
            Cell(label="peer default", present=True, known=True, value=0),
            Cell(label="peer other", present=True, known=True, value=3),
            Cell(label="peer same", present=True, known=True, value=2),
        ),
        control=SettingControl("merge_wip", shipped_defaults_table()),
    )
    assert row.readings[1].apply_url == reverse("dash:settings_restore", args=["merge_wip"])
    assert row.readings[2].apply_url == reverse("dash:settings_set", args=["merge_wip"])
    assert row.readings[3].apply_url == ""


def test_a_seed_peer_equal_to_the_shipped_value_needs_manual_edit() -> None:
    row = CompareRow(
        surface=SEED,
        scope="loops",
        entry="scanning-news",
        title="description",
        subtitle="scanning-news",
        cells=(
            Cell(
                label="local",
                present=True,
                known=True,
                value="custom",
                default_value="shipped",
                default_recorded=True,
                local=True,
            ),
            Cell(label="peer shipped", present=True, known=True, value="shipped"),
            Cell(label="peer custom", present=True, known=True, value="other"),
        ),
        seed_shipped=True,
    )
    assert row.readings[1].apply_url == ""
    assert row.readings[2].apply_url == reverse(
        "dash:settings_seed_set", args=["loops", "scanning-news", "description"]
    )


def test_use_here_requires_an_import_or_clear_disposition() -> None:
    base = CompareRow(
        surface=SETTING,
        scope="",
        title="merge_wip",
        subtitle="global scope",
        cells=(
            Cell(label="local", present=False, known=True, default_value=0, default_recorded=True, local=True),
            Cell(label="peer", present=True, known=True, value=3),
        ),
        control=SettingControl("merge_wip", shipped_defaults_table()),
    )

    assert base.outcome.disposition is Disposition.IMPORT
    assert base.readings[1].apply_url
    clear = replace(
        base,
        cells=(replace(base.cells[0], default_value=3), base.cells[1]),
        equals_shipped_default=True,
    )
    assert clear.outcome.disposition is Disposition.CLEAR
    assert not clear.readings[1].apply_url
    assert replace(base, env_shadowed=True, source="db").readings[1].apply_url
    for changed, disposition in (
        (replace(base, syncable=False), Disposition.MANUAL),
        (replace(base, env_shadowed=True, source="env"), Disposition.SHADOWED),
        (replace(base, cells=(base.cells[0], replace(base.cells[1], value=None))), Disposition.BLOCKED),
    ):
        assert changed.outcome.disposition is disposition
        assert changed.writable
        assert not changed.readings[1].apply_url
