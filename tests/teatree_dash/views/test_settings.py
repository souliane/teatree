"""The settings page: section nav + detail pane, the POSTs, masking, and the page's size.

The page renders ONE section at a time. Two things follow that a whole-page test cannot
express and are asserted here instead: the nav offers every section and each section's pane
is reachable on its own, and the page's SIZE — form / input / CSRF-token counts — stays down
where it was 272 forms, 1,060 inputs and 271 tokens.
"""

import io
import re
from unittest.mock import patch

from django.test import TestCase
from django.urls import resolve, reverse

from teatree.config.cold_defaults import shipped_defaults_table
from teatree.config.schema import TeatreeSettingsSchema
from teatree.config.setting_groups import UNGROUPED_PATH, setting_group_path
from teatree.core.models import ConfigSetting
from teatree.dash.settings_editor import SettingsEditorView, SettingsGroupView, SettingsSection, build_settings_sections
from teatree.dash.settings_readouts import ReadoutsView
from teatree.dash.views import settings_readouts as exported_readouts_view
from teatree.dash.views.settings import (
    MAX_IMPORT_BYTES,
    SAFETY_CONFIRM_PHRASE,
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
_SAFETY_TOML = '[teatree]\nautonomy = "babysit"\n'

#: The page's whole DB cost: the readouts' reads plus the scope picker's DISTINCT, and the
#: ONE settings read every row of the pane is resolved from. Constant in the row count, and
#: two lower than before the request-scoped memo collapsed the repeated settings resolution.
_PAGE_QUERIES = 5
_PANE_QUERIES = 1
_READOUTS_QUERIES = 3


def _upload(text: str, name: str = "config.toml") -> io.BytesIO:
    upload = io.BytesIO(text.encode("utf-8"))
    upload.name = name
    return upload


def _section_slug(key: str) -> str:
    path = setting_group_path(key)
    return next(section.slug for section in build_settings_sections() if section.path == path)


def _import_block(body: str) -> str:
    """The import-result region only — the pane above it may mention the same key."""
    start = body.index('class="import-result"')
    return body[start : body.index("</section>", start)]


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
        assert set(ctx) == {"nav_items", "nav_active", "instance_label", "readouts", "editor", "confirm_phrase"}
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

    def test_the_pane_keeps_the_scope_on_every_row_url(self) -> None:
        body = self._body("?scope=proj")
        assert "?scope=proj" in body

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
    letting the body's ``hx-headers`` carry ONE CSRF token is what takes those down. The
    numbers are asserted as ceilings so a regression toward the old page turns this red.
    """

    def _body(self) -> str:
        ConfigSetting.objects.set_value("mode", "auto")  # an override → a Restore control too
        url = f"{reverse('dash:settings')}?section={_section_slug('mode')}"
        return self.client.get(url, **_LOOPBACK).content.decode()

    def _pane(self) -> str:
        body = self._body()
        return body[body.index('id="settings-pane"') : body.index("<h2>Export</h2>")]

    def test_the_page_carries_exactly_one_csrf_token(self) -> None:
        # The import upload needs a real form field; every htmx POST rides the body's
        # hx-headers instead, which is the pattern the terminal button already uses.
        assert self._body().count("csrfmiddlewaretoken") == 1

    def test_no_row_carries_a_hidden_input(self) -> None:
        assert 'type="hidden"' not in self._pane()

    def test_the_row_urls_carry_the_key_so_the_row_needs_no_field(self) -> None:
        body = self._body()
        assert reverse("dash:settings_set", args=["mode"]) in body
        assert reverse("dash:settings_restore", args=["mode"]) in body
        assert 'name="key"' not in body

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

    def test_a_scoped_write_redirects_back_keeping_the_scope(self) -> None:
        response = self._set("mode", {"value": '"auto"'}, scope="proj")
        assert response.status_code == 302
        assert response["Location"].endswith("?scope=proj")
        assert ConfigSetting.objects.get_effective("mode", scope="proj") == "auto"


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
    def _restore(self, key: str):
        return self.client.post(reverse("dash:settings_restore", args=[key]), **_LOOPBACK)

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


class TestHtmxRowSwap(TestCase):
    """A toggle swaps its own row — no redirect, no second full-page render, no scroll jump."""

    def _post(self, route: str, key: str, data: dict[str, str] | None = None, scope: str = ""):
        url = reverse(route, args=[key])
        return self.client.post(
            f"{url}?scope={scope}" if scope else url, data or {}, HTTP_HX_REQUEST="true", **_LOOPBACK
        )

    def test_the_page_wires_each_row_form_to_swap_only_its_own_row(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")  # an override → the restore control renders too
        url = f"{reverse('dash:settings')}?section={_section_slug('mode')}"
        body = self.client.get(url, **_LOOPBACK).content.decode()
        assert f'hx-post="{reverse("dash:settings_set", args=["mode"])}"' in body
        assert f'hx-post="{reverse("dash:settings_restore", args=["mode"])}"' in body
        assert 'hx-target="closest tr"' in body

    def test_an_htmx_set_answers_the_row_alone_never_a_redirect_or_a_full_page(self) -> None:
        response = self._post("dash:settings_set", "mode", {"value": '"auto"'})
        body = response.content.decode()
        assert response.status_code == 200
        assert body.lstrip().startswith("<tr")
        assert "<html" not in body
        assert "dash-nav" not in body

    def test_the_swapped_row_reflects_the_value_just_written(self) -> None:
        body = self._post("dash:settings_set", "mode", {"value": '"auto"'}).content.decode()
        assert ">auto<" in body
        assert reverse("dash:settings_restore", args=["mode"]) in body  # now overridden

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


class TestSettingsExport(TestCase):
    def test_export_downloads_a_dump_withholding_secrets(self) -> None:
        ConfigSetting.objects.set_value("banned_brands", ["synthetic"])
        ConfigSetting.objects.set_value("mode", "auto")
        response = self.client.get(reverse("dash:settings_export"), **_LOOPBACK)
        body = response.content.decode()
        assert response.status_code == 200
        assert response["Content-Disposition"] == 'attachment; filename="teatree-config.toml"'
        assert "synthetic" not in body
        assert "mode" in body

    def test_the_page_offers_both_filters_unticked(self) -> None:
        body = self.client.get(reverse("dash:settings"), **_LOOPBACK).content.decode()
        for name in ("default_keys_only", "include_defaults"):
            control = body[body.index(f'name="{name}"') - 60 : body.index(f'name="{name}"') + 60]
            assert 'type="checkbox"' in control, name
            assert "checked" not in control, name

    def test_both_filters_download_the_defaults_shape_under_its_own_name(self) -> None:
        url = f"{reverse('dash:settings_export')}?default_keys_only=1&include_defaults=1"
        response = self.client.get(url, **_LOOPBACK)
        body = response.content.decode()
        assert response["Content-Disposition"] == 'attachment; filename="defaults.toml"'
        assert body.startswith("# teatree shipped defaults")
        assert "merge_wip" in body


class TestSettingsImportTakesAFile(TestCase):
    """Import is a file upload — the operator picks the file they exported."""

    def _post(self, text: str, **extra: str):
        return self.client.post(reverse("dash:settings_import"), {"toml_file": _upload(text), **extra}, **_LOOPBACK)

    def test_the_page_offers_a_file_input_not_a_textarea(self) -> None:
        body = self.client.get(reverse("dash:settings"), **_LOOPBACK).content.decode()
        assert 'type="file"' in body
        assert 'name="toml_file"' in body
        assert 'enctype="multipart/form-data"' in body
        assert "<textarea" not in body

    def test_dry_run_preview_writes_nothing(self) -> None:
        response = self._post('[teatree]\nmode = "auto"\n', apply="")
        assert response.status_code == 200
        assert "dry-run" in response.content.decode()
        assert ConfigSetting.objects.count() == 0

    def test_apply_writes_the_rows(self) -> None:
        self._post('[teatree]\nmode = "interactive"\n', apply="1")
        assert ConfigSetting.objects.get_effective("mode") == "interactive"

    def test_a_nested_file_imports_exactly_as_a_flat_one_does(self) -> None:
        self._post('[teatree.Agents."Mode & harness"]\nmode = "interactive"\n', apply="1")
        assert ConfigSetting.objects.get_effective("mode") == "interactive"

    def test_no_file_is_refused_with_a_reason_rather_than_a_crash(self) -> None:
        response = self.client.post(reverse("dash:settings_import"), {"apply": ""}, **_LOOPBACK)
        assert response.status_code == 400
        assert "choose a .toml file" in response.content.decode()

    def test_a_non_utf8_file_is_refused_with_a_reason(self) -> None:
        upload = io.BytesIO(b"\xff\xfe\x00binary")
        upload.name = "config.toml"
        response = self.client.post(reverse("dash:settings_import"), {"toml_file": upload}, **_LOOPBACK)
        assert response.status_code == 400
        assert "not UTF-8" in response.content.decode()

    def test_an_oversized_file_is_refused_before_it_is_parsed(self) -> None:
        response = self._post("#" * (MAX_IMPORT_BYTES + 1))
        assert response.status_code == 400
        assert "import limit" in response.content.decode()

    def test_malformed_toml_is_refused_with_a_reason(self) -> None:
        response = self._post("[teatree\nmode = ")
        assert response.status_code == 400
        assert "invalid TOML" in response.content.decode()

    def test_a_safety_posture_key_is_not_written_without_the_confirm_phrase(self) -> None:
        response = self._post(_SAFETY_TOML, apply="1")
        assert ConfigSetting.objects.get_effective("autonomy") is None
        assert "safety-posture" in response.content.decode()

    def test_a_safety_posture_key_is_written_with_the_confirm_phrase(self) -> None:
        self._post(_SAFETY_TOML, apply="1", confirm=SAFETY_CONFIRM_PHRASE)
        assert ConfigSetting.objects.get_effective("autonomy") == "babysit"

    def test_the_dry_run_preview_flags_the_safety_posture_row(self) -> None:
        response = self._post(_SAFETY_TOML, apply="")
        block = _import_block(response.content.decode())
        assert "autonomy" in block
        assert "safety-posture" in block
        assert ConfigSetting.objects.count() == 0

    def test_apply_with_a_rejected_row_writes_nothing(self) -> None:
        response = self._post('[teatree]\nnot_a_setting = 1\nmode = "auto"\n', apply="1")
        assert response.status_code == 200
        assert "rejected" in response.content.decode()
        assert ConfigSetting.objects.count() == 0


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
    """The ``?scope=`` parameter is reachable from the UI, and an import keeps it."""

    def test_the_page_offers_a_scope_picker(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "scoped", scope="demo-overlay")
        body = self.client.get(reverse("dash:settings")).content.decode()
        assert 'name="scope"' in body
        assert "demo-overlay" in body

    def test_the_picker_offers_the_global_scope_and_every_scope_holding_rows(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "a", scope="alpha")
        ConfigSetting.objects.set_value("issue_implementer_label", "b", scope="beta")
        from teatree.dash.settings_editor import available_scopes  # noqa: PLC0415 — deferred: one assertion

        scopes = available_scopes()
        assert scopes[0] == ""
        assert {"alpha", "beta"} <= set(scopes)

    def test_an_import_re_renders_the_scope_the_operator_was_editing(self) -> None:
        """The file's own tables decide each row's scope; the PAGE scope decides the view."""
        ConfigSetting.objects.set_value("issue_implementer_label", "scoped", scope="demo-overlay")
        response = self.client.post(
            reverse("dash:settings_import"),
            {
                "toml_file": _upload('[teatree]\nissue_implementer_label = "x"\n'),
                "scope": "demo-overlay",
                "apply": "",
            },
        )
        assert response.status_code == 200
        assert response.context["editor"].scope == "demo-overlay"
