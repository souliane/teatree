"""The settings page: section nav + detail pane, the POSTs, masking, and the page's size.

The page renders ONE section at a time. Two things follow that a whole-page test cannot
express and are asserted here instead: the nav offers every section and each section's pane
is reachable on its own, and the page's SIZE — form / input / CSRF-token counts — stays down
where it was 272 forms, 1,060 inputs and 271 tokens.
"""

import re
from unittest.mock import patch

from django.test import TestCase
from django.urls import resolve, reverse

from teatree.config.cold_defaults import DEFAULTS_TOML, shipped_defaults_table
from teatree.config.enums import Autonomy
from teatree.config.schema import TeatreeSettingsSchema
from teatree.config.setting_groups import UNGROUPED_PATH, setting_comment, setting_group_path
from teatree.config.setting_help import setting_help
from teatree.core.models import ConfigSetting
from teatree.dash.settings_editor import (
    SettingsEditorView,
    SettingsGroupView,
    SettingsSection,
    available_scopes,
    build_setting_row,
    build_settings_editor,
    build_settings_group,
    build_settings_sections,
)
from teatree.dash.settings_readouts import ReadoutsView
from teatree.dash.views import settings_readouts as exported_readouts_view
from teatree.dash.views.base import SAFETY_CONFIRM_PHRASE
from teatree.dash.views.settings import (
    ReadoutsContext,
    SettingsGroupContext,
    SettingsPageContext,
    _page_context,
    _readouts_context,
    settings,
    settings_group,
    settings_readouts,
)

_ROW_ID = re.compile(r'id="setting-([a-z0-9_]+)"')
_H2 = re.compile(r"<h2[^>]*>(.*?)</h2>", re.DOTALL)

#: The live-readout panels the retired config page contributed, by heading.
_READOUT_HEADINGS = ("Model &amp; reasoning effort", "Credentials", "Self-repairs")

_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

#: The page's whole DB cost: the readouts' reads, the scope-column DISTINCT, and ONE settings
#: read PER SCOPE that every cell in that column resolves from. Constant in the number of
#: SETTINGS — the grid renders N scopes per row, so a per-cell read would be exactly the N+1
#: these pins exist to catch — and it grows only with the number of scopes.
_PAGE_QUERIES = 7
_PANE_QUERIES = 4
_READOUTS_QUERIES = 3


def _section_slug(key: str) -> str:
    path = setting_group_path(key)
    return next(section.slug for section in build_settings_sections() if section.path == path)


def _row_html(client, key: str) -> str:
    """One setting's rendered ``<tr>``, fetched through its own section's pane."""
    body = client.get(reverse("dash:settings_group", args=[_section_slug(key)]), **_LOOPBACK).content.decode()
    return body[body.index(f'id="setting-{key}"') :].split("</tr>")[0]


class TestSettingsPage(TestCase):
    def test_page_renders_and_is_in_the_nav(self) -> None:
        response = self.client.get(reverse("dash:settings"), **_LOOPBACK)
        assert response.status_code == 200
        assert "Settings" in response.content.decode()
        # reachable from another page's nav bar
        other = self.client.get(reverse("dash:health"), **_LOOPBACK)
        assert reverse("dash:settings") in other.content.decode()

    def test_route_is_registered(self) -> None:
        assert resolve(reverse("dash:settings")).func is settings

    def test_the_group_route_is_registered(self) -> None:
        slug = _section_slug("mode")
        assert resolve(reverse("dash:settings_group", args=[slug])).func is settings_group

    def test_the_readouts_route_is_registered_from_the_views_package_export(self) -> None:
        assert resolve(reverse("dash:settings_readouts")).func is settings_readouts is exported_readouts_view


class TestSettingsPageContext(TestCase):
    """The page context declares its shape — one typed dict, not a bag of strings."""

    def test_the_readouts_context_carries_only_the_readouts_view(self) -> None:
        assert ReadoutsContext.__annotations__ == {"readouts": ReadoutsView}
        ctx = _readouts_context()
        assert set(ctx) == {"readouts"}
        assert isinstance(ctx["readouts"], ReadoutsView)

    def test_the_page_context_carries_the_editor_beside_the_readouts_and_the_nav(self) -> None:
        assert SettingsPageContext.__annotations__["editor"] is SettingsEditorView
        ctx = _page_context()
        assert set(ctx) == {
            "nav_items",
            "nav_active",
            "instance_label",
            "brand_logo",
            "readouts",
            "editor",
            "confirm_phrase",
        }
        assert ctx["nav_active"] == "dash:settings"
        assert ctx["confirm_phrase"] == SAFETY_CONFIRM_PHRASE

    def test_the_group_context_carries_the_pane_alone_without_the_page_frame(self) -> None:
        # The right pane answers a section swap, so it must NOT rebuild the nav or the
        # readouts — that is what makes switching sections one small fragment.
        assert SettingsGroupContext.__annotations__["group"] is SettingsGroupView
        assert set(SettingsGroupContext.__annotations__) == {"group", "confirm_phrase"}

    def test_a_configured_secret_value_never_appears_in_the_response_bytes(self) -> None:
        # THE critical guarantee: a stored secret is masked before the context is built.
        ConfigSetting.objects.set_value("banned_terms", ["supersecretcodename"])
        section = _section_slug("banned_terms")
        response = self.client.get(f"{reverse('dash:settings')}?section={section}", **_LOOPBACK)
        body = response.content.decode()
        assert response.status_code == 200
        assert "supersecretcodename" not in body
        # the row is still present, masked
        assert "banned_terms" in body


class TestTheLeftNavAndRightPane(TestCase):
    """Sections on the LEFT, the selected section's rows on the RIGHT."""

    def _body(self, query: str = "") -> str:
        return self.client.get(f"{reverse('dash:settings')}{query}", **_LOOPBACK).content.decode()

    def test_the_nav_offers_every_section_and_the_pane_holds_one(self) -> None:
        body = self._body()
        sections = build_settings_sections()
        for section in sections:
            assert reverse("dash:settings_group", args=[section.slug]) in body, section.slug
        rendered = set(_ROW_ID.findall(body))
        assert rendered == set(_pane_keys(sections[0].slug))

    def test_the_nav_targets_the_detail_pane_never_the_document(self) -> None:
        body = self._body()
        assert 'hx-target="#settings-pane"' in body
        assert 'id="settings-pane"' in body

    def test_selecting_a_section_answers_that_section_alone(self) -> None:
        slug = _section_slug("require_merge_evidence")
        body = self.client.get(reverse("dash:settings_group", args=[slug]), **_LOOPBACK).content.decode()
        assert "<html" not in body
        assert set(_ROW_ID.findall(body)) == set(_pane_keys(slug))
        assert "mode" not in set(_ROW_ID.findall(body))

    def test_every_schema_key_is_reachable_through_exactly_one_section(self) -> None:
        # The regression guard for the 130-of-184 keys the retired band classifier dropped,
        # held across sections now that the page renders one at a time.
        reachable = [key for section in build_settings_sections() for key in _pane_keys(section.slug)]
        assert sorted(reachable) == sorted(TeatreeSettingsSchema.model_fields)
        assert len(reachable) == len(set(reachable))

    def test_the_section_query_parameter_selects_the_pane(self) -> None:
        slug = _section_slug("require_merge_evidence")
        body = self._body(f"?section={slug}")
        assert set(_ROW_ID.findall(body)) == set(_pane_keys(slug))

    def test_every_scope_gets_its_own_column_and_its_own_write_url(self) -> None:
        # One row, every scope on it: the global cell posts unscoped and the overlay cell
        # posts to that overlay, so an edit lands where the column says it does.
        ConfigSetting.objects.set_value("issue_implementer_label", "scoped", scope="proj")
        body = self._body(f"?section={_section_slug('mode')}")
        set_url = reverse("dash:settings_set", args=["mode"])
        assert f'hx-post="{set_url}"' in body
        assert f'hx-post="{set_url}?scope=proj"' in body
        assert ">proj</th>" in body
        assert ">global</th>" in body

    def test_a_key_no_group_declares_gets_a_visible_leftovers_section(self) -> None:
        declared = setting_group_path
        with patch(
            "teatree.config.setting_groups.setting_group_path",
            side_effect=lambda key: UNGROUPED_PATH if key == "mode" else declared(key),
        ):
            leftovers = next(s for s in build_settings_sections() if s.path == UNGROUPED_PATH)
            body = self.client.get(reverse("dash:settings_group", args=[leftovers.slug]), **_LOOPBACK).content.decode()
        assert 'id="setting-mode"' in body
        assert "no declared group" in body

    def test_the_page_absorbs_the_config_pages_live_readouts(self) -> None:
        body = self._body()
        for heading in _READOUT_HEADINGS:
            assert heading in body, heading
        assert "session_model" in body

    def test_the_readouts_keep_their_own_poll_so_live_values_stay_fresh(self) -> None:
        body = self._body()
        assert reverse("dash:settings_readouts") in body
        assert 'hx-trigger="every 15s"' in body

    def test_no_section_heading_repeats_a_readout_heading(self) -> None:
        headings = [" ".join(h.split()) for h in _H2.findall(self._body())]
        for readout in _READOUT_HEADINGS:
            assert headings.count(readout) == 1, f"{readout!r} heads more than one panel: {headings}"

    def test_the_readouts_fragment_is_pollable_on_its_own(self) -> None:
        response = self.client.get(reverse("dash:settings_readouts"), **_LOOPBACK)
        body = response.content.decode()
        assert response.status_code == 200
        assert "Self-repairs" in body
        assert "<html" not in body

    def test_the_two_masking_questions_stay_separate_now_they_share_one_page(self) -> None:
        # A credential coordinate answers two different questions, and merging the surfaces
        # must not collapse them: the READOUT shows which account and whether it resolves
        # (masked only when the coordinate NAME can carry an internal namespace), while the
        # editable ROW for the same key masks its VALUE under the is_secret taxonomy.
        key = "openai_compatible_credential_entry"
        ConfigSetting.objects.set_value(key, "router/key")
        body = self._body(f"?section={_section_slug(key)}")
        readout, _, rows = body.partition('id="setting-')
        assert "router/key" in readout
        assert "router/key" not in rows
        assert "***" in body[body.index(f'id="setting-{key}"') :][:600]


def _pane_keys(slug: str) -> list[str]:
    from teatree.dash.settings_editor import build_settings_group  # noqa: PLC0415 — deferred: test-local helper

    return [row.name for row in build_settings_group(slug).settings]


class TestThePageIsSmall(TestCase):
    """The measured defect: 272 forms, 1,060 inputs, 812 hidden fields, 271 CSRF tokens.

    Rendering one section at a time, moving ``key``/``scope`` into the ``hx-post`` URL and
    letting the body's ``hx-headers`` carry the CSRF token is what takes those down. The
    numbers are asserted as ceilings so a regression toward the old page turns this red.
    """

    def _body(self) -> str:
        ConfigSetting.objects.set_value("mode", "auto")  # an override → a Restore control too
        url = f"{reverse('dash:settings')}?section={_section_slug('mode')}"
        return self.client.get(url, **_LOOPBACK).content.decode()

    def _pane(self) -> str:
        body = self._body()
        return body[body.index('id="settings-pane"') : body.index("<h2>Import / export</h2>")]

    def test_the_page_carries_no_form_level_csrf_token(self) -> None:
        # The one real form left was the import upload, and that moved to its own page
        # (#4340); every write here is an htmx POST riding the body's hx-headers.
        assert self._body().count("csrfmiddlewaretoken") == 0

    def test_no_row_carries_a_hidden_input(self) -> None:
        assert 'type="hidden"' not in self._pane()

    def test_the_row_urls_carry_the_key_so_the_row_needs_no_field(self) -> None:
        body = self._body()
        assert reverse("dash:settings_set", args=["mode"]) in body
        assert 'name="key"' not in body

    def test_no_row_offers_a_restore_control(self) -> None:
        # Click-to-edit made restoring the same gesture as editing — emptying the cell — so
        # a control of its own would be a second way to do one thing.
        ConfigSetting.objects.set_value("mode", "auto")
        assert reverse("dash:settings_restore", args=["mode"]) not in self._body()
        assert "Restore default" not in self._body()

    def test_the_page_stays_far_below_the_measured_wall(self) -> None:
        body = self._body()
        assert body.count("<form") <= 40
        assert body.count("<input") <= 80
        assert len(body.encode()) <= 80_000


class TestThePageCostsNoQueryPerRow(TestCase):
    """The page is polled, so a per-row query would multiply with every tick.

    Pinned as an EQUALITY between the smallest and the largest section rather than a
    ceiling: a ceiling generous enough to survive a schema growing keys is generous
    enough to hide the N+1 it exists to catch.
    """

    def _sections(self) -> tuple[SettingsSection, SettingsSection]:
        by_size = sorted(build_settings_sections(), key=lambda section: section.key_count)
        smallest, largest = by_size[0], by_size[-1]
        # The control: without a real size spread the equality below proves nothing.
        assert largest.key_count >= smallest.key_count * 2
        return smallest, largest

    def test_the_page_costs_the_same_whatever_the_section_size(self) -> None:
        smallest, largest = self._sections()
        with self.assertNumQueries(_PAGE_QUERIES):
            self.client.get(f"{reverse('dash:settings')}?section={smallest.slug}", **_LOOPBACK)
        with self.assertNumQueries(_PAGE_QUERIES):
            self.client.get(f"{reverse('dash:settings')}?section={largest.slug}", **_LOOPBACK)

    def test_a_section_swap_costs_one_query_whatever_its_size(self) -> None:
        smallest, largest = self._sections()
        for section in (smallest, largest):
            with self.assertNumQueries(_PANE_QUERIES):
                self.client.get(reverse("dash:settings_group", args=[section.slug]), **_LOOPBACK)

    def test_the_polled_readouts_fragment_costs_a_fixed_number(self) -> None:
        # The 15s poll — the one request that repeats for as long as the page is open.
        with self.assertNumQueries(_READOUTS_QUERIES):
            self.client.get(reverse("dash:settings_readouts"), **_LOOPBACK)

    def test_an_override_row_does_not_add_a_query(self) -> None:
        _, largest = self._sections()
        url = f"{reverse('dash:settings')}?section={largest.slug}"
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("merge_wip", 4)
        with self.assertNumQueries(_PAGE_QUERIES):
            self.client.get(url, **_LOOPBACK)


class TestConfigPageIsRetired(TestCase):
    def test_the_old_config_url_redirects_to_the_one_settings_page(self) -> None:
        response = self.client.get(reverse("dash:config"), **_LOOPBACK)
        assert response.status_code == 302
        assert response["Location"] == reverse("dash:settings")

    def test_the_nav_no_longer_offers_a_separate_config_page(self) -> None:
        body = self.client.get(reverse("dash:health"), **_LOOPBACK).content.decode()
        assert ">Config<" not in body


class TestSettingsSet(TestCase):
    def _set(self, key: str, data: dict[str, str], scope: str = ""):
        url = reverse("dash:settings_set", args=[key])
        return self.client.post(f"{url}?scope={scope}" if scope else url, data, **_LOOPBACK)

    def test_set_writes_the_canonical_value_to_the_db(self) -> None:
        self._set("mode", {"value": '"auto"'})
        assert ConfigSetting.objects.get_effective("mode") == "auto"

    def test_set_rejects_an_unknown_key(self) -> None:
        response = self._set("not_a_setting", {"value": "1"})
        assert response.status_code == 400
        assert ConfigSetting.objects.count() == 0

    def test_set_rejects_an_invalid_value(self) -> None:
        response = self._set("issue_implementer_enabled", {"value": '"false"'})
        assert response.status_code == 400
        assert ConfigSetting.objects.count() == 0

    def test_a_secret_is_settable_but_its_value_stays_off_the_page(self) -> None:
        self._set("banned_terms", {"value": '["hushword"]'})
        assert ConfigSetting.objects.get_effective("banned_terms") == ["hushword"]
        section = _section_slug("banned_terms")
        body = self.client.get(f"{reverse('dash:settings')}?section={section}", **_LOOPBACK).content.decode()
        assert "hushword" not in body

    def test_set_rejects_a_non_json_value(self) -> None:
        response = self._set("mode", {"value": "not-json"})
        assert response.status_code == 400
        assert "invalid JSON" in response.content.decode()
        assert ConfigSetting.objects.count() == 0

    def test_set_rejects_a_cross_field_inconsistent_value(self) -> None:
        # Coerces fine, but set_value's #258 consistency check refuses the pair.
        response = self._set("agent_harness_provider", {"value": '"openai_compatible"'})
        assert response.status_code == 400
        assert "inconsistent config" in response.content.decode()
        assert ConfigSetting.objects.count() == 0

    def test_a_scoped_write_lands_in_the_scope_its_column_names(self) -> None:
        response = self._set("mode", {"value": '"auto"'}, scope="proj")
        assert response.status_code == 302
        assert ConfigSetting.objects.get_effective("mode", scope="proj") == "auto"
        assert ConfigSetting.objects.get_effective("mode") is None

    def test_an_emptied_cell_clears_that_scopes_row_so_the_default_resolves_again(self) -> None:
        # The click-to-edit restore gesture: there is no restore button, so an empty value
        # IS the way back to the default, and it clears only the column it was posted from.
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("mode", "auto", scope="proj")
        assert self._set("mode", {"value": ""}, scope="proj").status_code == 302
        assert ConfigSetting.objects.get_effective("mode", scope="proj") is None
        assert ConfigSetting.objects.get_effective("mode") == "auto"


class TestSafetyPostureConfirm(TestCase):
    def _set(self, key: str, data: dict[str, str]):
        return self.client.post(reverse("dash:settings_set", args=[key]), data, **_LOOPBACK)

    def test_a_safety_posture_key_is_refused_without_the_confirm_phrase(self) -> None:
        response = self._set("enforce_regulated_path", {"value": "true"})
        assert response.status_code == 400
        assert ConfigSetting.objects.get_effective("enforce_regulated_path") is None

    def test_a_safety_posture_key_writes_with_the_confirm_phrase(self) -> None:
        self._set("enforce_regulated_path", {"value": "true", "confirm": SAFETY_CONFIRM_PHRASE})
        assert ConfigSetting.objects.get_effective("enforce_regulated_path") is True


class TestSettingsRestore(TestCase):
    def _restore(self, key: str, data: dict[str, str] | None = None):
        return self.client.post(reverse("dash:settings_restore", args=[key]), data or {}, **_LOOPBACK)

    def test_restore_refuses_a_safety_posture_key_without_the_confirm_phrase(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        assert self._restore("enforce_regulated_path").status_code == 400
        assert ConfigSetting.objects.get_effective("enforce_regulated_path") is True

    def test_restore_deletes_a_safety_posture_key_with_the_confirm_phrase(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        self._restore("enforce_regulated_path", {"confirm": SAFETY_CONFIRM_PHRASE})
        assert ConfigSetting.objects.get_effective("enforce_regulated_path") is None

    def test_restore_deletes_the_db_row(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        assert ConfigSetting.objects.get_effective("mode") == "auto"
        self._restore("mode")
        assert ConfigSetting.objects.get_effective("mode") is None

    def test_restore_rejects_an_unknown_key_instead_of_silently_no_opping(self) -> None:
        assert self._restore("not_a_setting").status_code == 400

    def test_restoring_an_unset_key_audits_nothing_but_still_answers(self) -> None:
        # Nothing was deleted, so there is no action to record — the page still answers.
        with self.assertNoLogs("teatree.dash.audit", level="INFO"):
            assert self._restore("mode").status_code == 302


class TestNoShippedDefaultDrift(TestCase):
    """A no-shipped-default key still has a real default: its code default.

    A Secret/Personal key `defaults.toml` does not carry, so an operator's own tier
    outranking that IS a drift, not a free pass. Regression for a cell that read "same as
    default" whenever the shipped file carried no entry at all, whatever the DB held.
    """

    def test_an_unset_no_default_key_matches_its_code_default(self) -> None:
        row = build_setting_row("workspace_dir")
        assert not row.has_shipped_default
        assert all(cell.matches_default for cell in row.cells)
        assert not row.drifts

    def test_an_overridden_no_default_key_reads_as_drifted(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "/srv/custom")
        row = build_setting_row("workspace_dir")
        cell = next(c for c in row.cells if c.scope == "")
        assert not cell.matches_default
        assert row.drifts

    def test_the_rendered_row_shows_the_drift_for_a_no_default_key(self) -> None:
        ConfigSetting.objects.set_value("workspace_dir", "/srv/custom")
        body = _row_html(self.client, "workspace_dir")
        assert "differs from default" in body


class TestSelectOffersRestoreOnlyWhenDrifted(TestCase):
    """A drifted select carries its own restore option, offered only once drifted.

    A <select> cannot be "emptied" by typing, so the click-to-edit restore gesture needs
    its own option once a select-rendered setting has drifted — and only then, mirroring
    the old page's restore button appearing only on an overridden row.
    """

    def test_a_select_at_its_default_offers_no_restore_option(self) -> None:
        body = _row_html(self.client, "mode")
        assert "restore default" not in body

    def test_a_drifted_select_offers_a_restore_option(self) -> None:
        ConfigSetting.objects.set_value("mode", "interactive")
        body = _row_html(self.client, "mode")
        assert '<option value="">' in body
        assert "restore default" in body

    def test_choosing_the_restore_option_clears_the_row(self) -> None:
        ConfigSetting.objects.set_value("mode", "interactive")
        url = reverse("dash:settings_set", args=["mode"])
        response = self.client.post(url, {"value": ""}, HTTP_HX_REQUEST="true", **_LOOPBACK)
        assert response.status_code == 200
        assert ConfigSetting.objects.get_effective("mode") is None
        assert "restore default" not in response.content.decode()


class TestHtmxRowSwap(TestCase):
    """A toggle swaps its own row — no redirect, no second full-page render, no scroll jump."""

    def _post(self, route: str, key: str, data: dict[str, str] | None = None, scope: str = ""):
        url = reverse(route, args=[key])
        return self.client.post(
            f"{url}?scope={scope}" if scope else url, data or {}, HTTP_HX_REQUEST="true", **_LOOPBACK
        )

    def test_the_page_wires_each_cell_to_swap_only_its_own_row(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        url = f"{reverse('dash:settings')}?section={_section_slug('mode')}"
        body = self.client.get(url, **_LOOPBACK).content.decode()
        assert f'hx-post="{reverse("dash:settings_set", args=["mode"])}"' in body
        assert 'hx-target="closest tr"' in body
        # No save button and no form: the control posts itself the moment it changes.
        assert 'hx-trigger="change"' in body
        assert "<form" not in body[body.index('id="setting-mode"') :][:2000]

    def test_an_htmx_set_answers_the_row_alone_never_a_redirect_or_a_full_page(self) -> None:
        response = self._post("dash:settings_set", "mode", {"value": '"auto"'})
        body = response.content.decode()
        assert response.status_code == 200
        assert body.lstrip().startswith("<tr")
        assert "<html" not in body
        assert "dash-nav" not in body

    def test_the_swapped_row_reflects_the_value_just_written(self) -> None:
        # ``interactive`` is not the shipped ``mode``, so the swapped row comes back with the
        # written option preselected AND the cell's verdict flipped to the drifted one.
        body = self._post("dash:settings_set", "mode", {"value": '"interactive"'}).content.decode()
        assert "&quot;interactive&quot;" in body
        assert "selected" in body
        assert "differs from default" in body

    def test_an_htmx_restore_answers_the_row_back_at_its_default(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        body = self._post("dash:settings_restore", "mode").content.decode()
        assert ConfigSetting.objects.get_effective("mode") is None
        assert body.lstrip().startswith("<tr")
        assert reverse("dash:settings_restore", args=["mode"]) not in body  # no row left to delete

    def test_a_secret_never_reaches_the_swapped_fragment(self) -> None:
        body = self._post("dash:settings_set", "banned_terms", {"value": '["hushword"]'}).content.decode()
        assert ConfigSetting.objects.get_effective("banned_terms") == ["hushword"]
        assert "hushword" not in body
        assert "***" in body

    def test_a_refused_write_answers_the_row_carrying_the_reason(self) -> None:
        response = self._post("dash:settings_set", "mode", {"value": "not-json"})
        body = response.content.decode()
        assert response.status_code == 400
        assert body.lstrip().startswith("<tr")
        assert "invalid JSON" in body
        assert ConfigSetting.objects.count() == 0

    def test_a_safety_posture_key_still_needs_the_confirm_phrase_over_htmx(self) -> None:
        response = self._post("dash:settings_set", "enforce_regulated_path", {"value": "true"})
        assert response.status_code == 400
        assert SAFETY_CONFIRM_PHRASE in response.content.decode()
        assert ConfigSetting.objects.get_effective("enforce_regulated_path") is None

    def test_a_scoped_htmx_write_keeps_the_scope_on_the_swapped_row(self) -> None:
        body = self._post("dash:settings_set", "mode", {"value": '"auto"'}, scope="proj").content.decode()
        assert ConfigSetting.objects.get_effective("mode", scope="proj") == "auto"
        assert "?scope=proj" in body

    def test_a_non_htmx_post_still_redirects_rather_than_answering_a_fragment(self) -> None:
        response = self.client.post(reverse("dash:settings_set", args=["mode"]), {"value": '"auto"'}, **_LOOPBACK)
        assert response.status_code == 302
        assert response["Location"] == reverse("dash:settings")


class TestShippedDefaultColumn(TestCase):
    """The pane shows each key's shipped default and where the effective value came from."""

    def _body(self, key: str = "mode") -> str:
        url = f"{reverse('dash:settings')}?section={_section_slug(key)}"
        return self.client.get(url, **_LOOPBACK).content.decode()

    def test_the_column_and_a_matching_verdict_render(self) -> None:
        body = self._body()
        assert "shipped default" in body
        assert "same as default" in body

    def test_the_provenance_column_replaces_the_near_constant_category_column(self) -> None:
        body = self._body()
        assert "value came from" in body
        assert "<th>category</th>" not in body

    def test_the_provenance_names_the_tier_that_actually_supplied_the_value(self) -> None:
        ConfigSetting.objects.set_value("mode", "interactive")
        body = self._body()
        row = body[body.index('id="setting-mode"') :][:900]
        assert "DB global scope" in row
        assert "differs from default" in row

    def test_an_override_renders_the_differing_verdict(self) -> None:
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 42)
        body = self._body("provision_ram_ceiling_percent")
        assert "differs from default" in body
        assert "default-differs" in body

    def test_the_verdict_is_never_colour_alone(self) -> None:
        # Each coloured span carries a text icon AND its own words, so the verdict survives
        # greyscale, colour blindness, and a screen reader.
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 42)
        body = self._body("provision_ram_ceiling_percent")
        for css_class, icon, words in (
            ("default-match", "&#10003;", "same as default"),
            ("default-differs", "&#9679;", "differs from default"),
        ):
            span = body[body.index(css_class) :][:200]
            assert icon in span, css_class
            assert words in span, css_class

    def test_a_secret_shipped_default_never_reaches_the_response_bytes(self) -> None:
        # The masking contract extends to this column. The shipped table is forced to carry
        # a value for a secret key, so an unmasked column would serialise it.
        ConfigSetting.objects.set_value("banned_terms", ["supersecretcodename"])
        leaky = {**shipped_defaults_table(), "banned_terms": ["anotherhushword"]}
        with patch("teatree.dash.settings_editor.shipped_defaults_table", return_value=leaky):
            body = self._body("banned_terms")
        assert "supersecretcodename" not in body
        assert "anotherhushword" not in body
        assert "banned_terms" in body


class SettingsScopeControlTestCase(TestCase):
    """Every scope is a COLUMN of the grid rather than something the operator switches to."""

    def test_the_page_gives_every_scope_its_own_column(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "scoped", scope="demo-overlay")
        body = self.client.get(reverse("dash:settings")).content.decode()
        assert 'name="scope"' not in body  # the picker is gone — nothing to switch between
        assert ">demo-overlay</th>" in body

    def test_the_picker_offers_the_global_scope_and_every_scope_holding_rows(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "a", scope="alpha")
        ConfigSetting.objects.set_value("issue_implementer_label", "b", scope="beta")
        from teatree.dash.settings_editor import available_scopes  # noqa: PLC0415 — deferred: one assertion

        scopes = available_scopes()
        assert scopes[0] == ""
        assert {"alpha", "beta"} <= set(scopes)


class TestTheGridIsOneRowPerSettingAcrossEveryScope(TestCase):
    """#3880: one row per setting, every scope on it, drift coloured against the default."""

    def _pane(self, key: str) -> str:
        url = reverse("dash:settings_group", args=[_section_slug(key)])
        return self.client.get(url, **_LOOPBACK).content.decode()

    def _row(self, key: str) -> str:
        body = self._pane(key)
        return body[body.index(f'id="setting-{key}"') :].split("</tr>")[0]

    def test_a_setting_renders_once_however_many_scopes_hold_a_row(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        ConfigSetting.objects.set_value("merge_wip", 7, scope="alpha")
        ConfigSetting.objects.set_value("merge_wip", 9, scope="beta")
        assert self._pane("merge_wip").count('id="setting-merge_wip"') == 1

    def test_the_row_carries_one_cell_per_scope_in_the_column_order(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 7, scope="alpha")
        row = self._row("merge_wip")
        assert row.count('class="setting-cell') == len(available_scopes())
        assert row.index("?scope=alpha") > row.index('hx-post="/dash/settings/set/merge_wip/"')

    def test_a_cell_equal_to_the_default_is_green_and_one_that_differs_is_brown(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4, scope="alpha")
        row = self._row("merge_wip")
        assert "cell-at-default" in row  # global still ships the default
        assert "cell-drifted" in row  # the overlay column does not

    def test_the_provenance_is_a_tooltip_not_a_column_of_its_own(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        body = self._pane("merge_wip")
        assert "value came from DB global scope" in self._row("merge_wip")
        assert "<th>value came from</th>" not in body
        assert "value came from</th>" not in body


class TestHelpTextIsAuthoredOnceAndRenderedHere(TestCase):
    def test_the_setting_name_carries_its_authored_sentence_as_a_tooltip(self) -> None:
        url = reverse("dash:settings_group", args=[_section_slug("merge_wip")])
        body = self.client.get(url, **_LOOPBACK).content.decode()
        assert f'title="{setting_help("merge_wip")}"' in body

    def test_every_rendered_row_carries_help_text(self) -> None:
        # The table is total over the schema, so no row can render with an empty tooltip.
        for section in build_settings_sections():
            group = build_settings_group(section.slug)
            assert all(row.help_text for row in group.settings), section.slug

    def test_the_tooltip_is_the_same_sentence_the_shipped_file_comments_the_key_with(self) -> None:
        # The file's comment is composed by `setting_comment` — what the key accepts, then what
        # it means — so the expectation is read from that one renderer rather than re-typed. The
        # tooltip is the help HALF of it, which is what this class is about.
        shipped = DEFAULTS_TOML.read_text(encoding="utf-8")
        assert f"merge_wip = 1 # {setting_comment('merge_wip')}" in shipped
        assert setting_help("merge_wip") in setting_comment("merge_wip")


class TestConstrainedTypesRenderAsSelects(TestCase):
    def test_an_enum_setting_renders_a_select_of_the_schema_s_own_members(self) -> None:
        row = _row_html(self.client, "autonomy")
        assert "<select" in row
        for member in Autonomy:
            assert f'value="&quot;{member.value}&quot;"' in row

    def test_a_boolean_setting_renders_a_select_never_a_text_box(self) -> None:
        row = _row_html(self.client, "autoload")
        assert "<select" in row
        assert 'value="true"' in row
        assert 'value="false"' in row
        assert 'type="text"' not in row

    def test_an_open_typed_setting_still_renders_free_text(self) -> None:
        # The control for the two above: a select is derived, not applied to everything.
        row = _row_html(self.client, "merge_wip")
        assert "<select" not in row
        assert 'type="text"' in row

    def test_a_closed_str_setting_renders_a_select_of_every_member_including_the_empty_one(self) -> None:
        row = _row_html(self.client, "repo_mode")
        assert "<select" in row
        assert 'type="text"' not in row
        for member in ("", "solo", "collaborative"):
            assert f'value="&quot;{member}&quot;"' in row

    def test_the_options_come_from_the_schema_rather_than_a_list_kept_beside_it(self) -> None:
        # Patched at the ONE derivation the grid composes, so the row cannot be offering a
        # second list of its own: teatree.core.setting_control is the only source of options.
        # Read inside the patch because a row's options are derived on access, not stored.
        with patch("teatree.core.setting_control.setting_choices", return_value=("only-this",)):
            assert [choice.label for choice in build_setting_row("autonomy").choices] == ["only-this"]


class TestTheNavCountsDriftedSettings(TestCase):
    def _section_of(self, key: str) -> SettingsSection:
        slug = _section_slug(key)
        return next(s for s in build_settings_editor(slug).sections if s.slug == slug)

    def test_a_section_with_nothing_changed_counts_zero(self) -> None:
        assert self._section_of("merge_wip").drift_count == 0

    def test_a_changed_setting_adds_one_to_its_section(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        assert self._section_of("merge_wip").drift_count == 1

    def test_a_setting_changed_in_several_scopes_still_counts_once(self) -> None:
        # The rule the count answers: how many settings here has someone changed, not how
        # many override rows exist.
        ConfigSetting.objects.set_value("merge_wip", 4)
        ConfigSetting.objects.set_value("merge_wip", 7, scope="alpha")
        ConfigSetting.objects.set_value("merge_wip", 9, scope="beta")
        assert self._section_of("merge_wip").drift_count == 1

    def test_two_changed_settings_in_one_section_count_two(self) -> None:
        section = _section_slug("merge_wip")
        keys = [k for k in _pane_keys(section) if k in {"merge_wip", "write_wip"}]
        assert len(keys) == 2, "the fixture needs two keys in one section"
        ConfigSetting.objects.set_value(keys[0], 4)
        ConfigSetting.objects.set_value(keys[1], 4)
        assert self._section_of("merge_wip").drift_count == 2

    def test_the_count_renders_in_the_nav(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        body = self.client.get(reverse("dash:settings"), **_LOOPBACK).content.decode()
        assert "settings-nav-drift" in body
        assert "setting(s) here differ from their default" in body
