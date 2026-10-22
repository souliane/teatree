from dataclasses import replace

from django.template.loader import get_template
from django.urls import reverse

from teatree.config.cold_defaults import shipped_defaults_table
from teatree.core.setting_control import SettingControl
from teatree.core.settings.settings_compare import CompareFilters, CompareView
from teatree.core.settings.settings_compare_rows import WITHHELD_REASON, Cell, CompareRow
from teatree.core.settings.settings_compare_sync import SETTING, Disposition
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
    assert row.can_apply
    restore_url = reverse("dash:settings_restore", args=["merge_wip"])
    assert f'class="compare-revert" hx-post="{restore_url}"' in rendered


def test_a_clear_disposition_uses_the_restore_endpoint() -> None:
    row = CompareRow(
        surface=SETTING,
        scope="",
        title="merge_wip",
        subtitle="global scope",
        cells=(
            Cell(label="local", present=True, known=True, value=True, local=True),
            Cell(label="peer", present=True, known=True, value=False),
        ),
        equals_shipped_default=True,
        control=SettingControl("merge_wip", shipped_defaults_table()),
    )
    assert row.outcome.disposition is Disposition.CLEAR
    assert row.apply_url == reverse("dash:settings_restore", args=["merge_wip"])


def test_use_here_requires_an_import_or_clear_disposition() -> None:
    base = CompareRow(
        surface=SETTING,
        scope="",
        title="merge_wip",
        subtitle="global scope",
        cells=(
            Cell(label="local", present=False, known=True, local=True),
            Cell(label="peer", present=True, known=True, value=3),
        ),
    )

    assert base.outcome.disposition is Disposition.IMPORT
    assert base.can_apply
    assert replace(base, equals_shipped_default=True).outcome.disposition is Disposition.CLEAR
    assert replace(base, equals_shipped_default=True).can_apply
    for changed, disposition in (
        (replace(base, syncable=False), Disposition.MANUAL),
        (replace(base, env_shadowed=True), Disposition.SHADOWED),
        (replace(base, cells=(base.cells[0], replace(base.cells[1], value=None))), Disposition.BLOCKED),
    ):
        assert changed.outcome.disposition is disposition
        assert changed.writable
        assert not changed.can_apply
