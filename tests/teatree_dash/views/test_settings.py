"""The settings-editor POSTs write through the validating seam, mask secrets, gate safety keys (D7)."""

import re
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.urls import resolve, reverse

from teatree.config.cold_defaults import shipped_defaults_table
from teatree.config.schema import TeatreeSettingsSchema, shipped_defaults
from teatree.config.setting_groups import group_labels
from teatree.core.models import ConfigSetting
from teatree.dash.settings_editor import SettingsEditorView, build_settings_editor
from teatree.dash.settings_readouts import ReadoutsView
from teatree.dash.views import settings_readouts as exported_readouts_view
from teatree.dash.views.settings import (
    SAFETY_CONFIRM_PHRASE,
    ReadoutsContext,
    SettingsPageContext,
    _page_context,
    _readouts_context,
    settings,
    settings_readouts,
)

_ROW_ID = re.compile(r'id="setting-([a-z0-9_]+)"')

_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}
_SAFETY_TOML = '[teatree]\nautonomy = "full"\n'


def _import_block(body: str) -> str:
    """The import-result region only — the editor table below it mentions every safety key."""
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

    def test_a_configured_secret_value_never_appears_in_the_response_bytes(self) -> None:
        # THE critical guarantee: a stored secret is masked before the context is built.
        ConfigSetting.objects.set_value("banned_terms", ["supersecretcodename"])
        response = self.client.get(reverse("dash:settings"), **_LOOPBACK)
        body = response.content.decode()
        assert response.status_code == 200
        assert "supersecretcodename" not in body
        # the row is still present, masked
        assert "banned_terms" in body


class TestTheOneSettingsPage(TestCase):
    """``/dash/config`` is absorbed here — one page carrying every key AND the live readouts."""

    def _body(self) -> str:
        return self.client.get(reverse("dash:settings"), **_LOOPBACK).content.decode()

    def test_no_schema_key_is_dropped_from_the_page(self) -> None:
        # The direct regression test for the 130-of-184 keys the retired band classifier
        # returned "" for and silently ``continue``d out of /dash/config.
        rendered = set(_ROW_ID.findall(self._body()))
        assert rendered == set(TeatreeSettingsSchema.model_fields)

    def test_every_key_sits_under_a_named_group_heading(self) -> None:
        body = self._body()
        headings = [label for label in group_labels() if f"<h2>{label}</h2>" in body]
        assert headings, "the page renders no group heading at all"
        # Each rendered row belongs to the group section that precedes it.
        for label in headings:
            section = body[body.index(f"<h2>{label}</h2>") :]
            assert _ROW_ID.search(section), f"group {label!r} renders a heading but no row"

    def test_the_page_absorbs_the_config_pages_live_readouts(self) -> None:
        body = self._body()
        for heading in ("Model &amp; reasoning effort", "Credentials", "Self-repairs"):
            assert heading in body, heading
        assert "session_model" in body

    def test_the_readouts_keep_their_own_poll_so_live_values_stay_fresh(self) -> None:
        body = self._body()
        assert reverse("dash:settings_readouts") in body
        assert 'hx-trigger="every 15s"' in body

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
        ConfigSetting.objects.set_value("openai_compatible_credential_entry", "router/key")
        body = self._body()
        readout, _, rows = body.partition('id="setting-')
        assert "router/key" in readout
        assert "router/key" not in rows
        row = body[body.index('id="setting-openai_compatible_credential_entry"') :][:600]
        assert "***" in row

    def test_a_key_no_group_declares_renders_under_a_visible_leftovers_banner(self) -> None:
        # The never-vanish guarantee, exercised: force a key out of every declared group.
        with patch("teatree.dash.settings_editor.setting_group", side_effect=lambda key: "" if key == "mode" else "x"):
            body = self._body()
        assert 'id="setting-mode"' in body
        assert "Ungrouped" in body
        assert "no declared group" in body


class TestConfigPageIsRetired(TestCase):
    def test_the_old_config_url_redirects_to_the_one_settings_page(self) -> None:
        response = self.client.get(reverse("dash:config"), **_LOOPBACK)
        assert response.status_code == 302
        assert response["Location"] == reverse("dash:settings")

    def test_the_nav_no_longer_offers_a_separate_config_page(self) -> None:
        body = self.client.get(reverse("dash:health"), **_LOOPBACK).content.decode()
        assert ">Config<" not in body


class TestSettingsSet(TestCase):
    def test_set_writes_the_canonical_value_to_the_db(self) -> None:
        self.client.post(reverse("dash:settings_set"), {"key": "mode", "value": '"auto"'}, **_LOOPBACK)
        assert ConfigSetting.objects.get_effective("mode") == "auto"

    def test_set_rejects_an_unknown_key(self) -> None:
        response = self.client.post(reverse("dash:settings_set"), {"key": "not_a_setting", "value": "1"}, **_LOOPBACK)
        assert response.status_code == 400
        assert ConfigSetting.objects.count() == 0

    def test_set_rejects_an_invalid_value(self) -> None:
        response = self.client.post(
            reverse("dash:settings_set"), {"key": "issue_implementer_enabled", "value": '"false"'}, **_LOOPBACK
        )
        assert response.status_code == 400
        assert ConfigSetting.objects.count() == 0

    def test_a_secret_is_settable_but_its_value_stays_off_the_page(self) -> None:
        self.client.post(reverse("dash:settings_set"), {"key": "banned_terms", "value": '["hushword"]'}, **_LOOPBACK)
        assert ConfigSetting.objects.get_effective("banned_terms") == ["hushword"]
        body = self.client.get(reverse("dash:settings"), **_LOOPBACK).content.decode()
        assert "hushword" not in body

    def test_set_rejects_a_non_json_value(self) -> None:
        response = self.client.post(reverse("dash:settings_set"), {"key": "mode", "value": "not-json"}, **_LOOPBACK)
        assert response.status_code == 400
        assert "invalid JSON" in response.content.decode()
        assert ConfigSetting.objects.count() == 0

    def test_set_rejects_a_cross_field_inconsistent_value(self) -> None:
        # Coerces fine, but set_value's #258 consistency check refuses the pair (harness != pydantic_ai).
        response = self.client.post(
            reverse("dash:settings_set"), {"key": "agent_harness_provider", "value": '"openai_compatible"'}, **_LOOPBACK
        )
        assert response.status_code == 400
        assert "inconsistent config" in response.content.decode()
        assert ConfigSetting.objects.count() == 0

    def test_a_scoped_write_redirects_back_keeping_the_scope(self) -> None:
        response = self.client.post(
            reverse("dash:settings_set"), {"key": "mode", "value": '"auto"', "scope": "proj"}, **_LOOPBACK
        )
        assert response.status_code == 302
        assert response["Location"].endswith("?scope=proj")
        assert ConfigSetting.objects.get_effective("mode", scope="proj") == "auto"


class TestSafetyPostureConfirm(TestCase):
    def test_a_safety_posture_key_is_refused_without_the_confirm_phrase(self) -> None:
        response = self.client.post(
            reverse("dash:settings_set"), {"key": "enforce_regulated_path", "value": "true"}, **_LOOPBACK
        )
        assert response.status_code == 400
        assert ConfigSetting.objects.get_effective("enforce_regulated_path") is None

    def test_a_safety_posture_key_writes_with_the_confirm_phrase(self) -> None:
        self.client.post(
            reverse("dash:settings_set"),
            {"key": "enforce_regulated_path", "value": "true", "confirm": SAFETY_CONFIRM_PHRASE},
            **_LOOPBACK,
        )
        assert ConfigSetting.objects.get_effective("enforce_regulated_path") is True


class TestSettingsRestore(TestCase):
    def test_restore_deletes_the_db_row(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        assert ConfigSetting.objects.get_effective("mode") == "auto"
        self.client.post(reverse("dash:settings_restore"), {"key": "mode"}, **_LOOPBACK)
        assert ConfigSetting.objects.get_effective("mode") is None

    def test_restore_rejects_an_unknown_key_instead_of_silently_no_opping(self) -> None:
        response = self.client.post(reverse("dash:settings_restore"), {"key": "not_a_setting"}, **_LOOPBACK)
        assert response.status_code == 400

    def test_restoring_an_unset_key_audits_nothing_but_still_answers(self) -> None:
        # Nothing was deleted, so there is no action to record — the page still answers.
        with self.assertNoLogs("teatree.dash.audit", level="INFO"):
            response = self.client.post(reverse("dash:settings_restore"), {"key": "mode"}, **_LOOPBACK)
        assert response.status_code == 302


class TestHtmxRowSwap(TestCase):
    """A toggle swaps its own row — no redirect, no second full-page render, no scroll jump."""

    def _post(self, route: str, data: dict[str, str]):
        return self.client.post(reverse(route), data, HTTP_HX_REQUEST="true", **_LOOPBACK)

    def test_the_page_wires_each_row_form_to_swap_only_its_own_row(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")  # an override → the restore form renders too
        body = self.client.get(reverse("dash:settings"), **_LOOPBACK).content.decode()
        assert 'hx-post="/dash/settings/set/"' in body
        assert 'hx-post="/dash/settings/restore/"' in body
        assert 'hx-target="closest tr"' in body

    def test_an_htmx_set_answers_the_row_alone_never_a_redirect_or_a_full_page(self) -> None:
        response = self._post("dash:settings_set", {"key": "mode", "value": '"auto"'})
        body = response.content.decode()
        assert response.status_code == 200
        assert body.lstrip().startswith("<tr")
        assert "<html" not in body
        assert "dash-nav" not in body

    def test_the_swapped_row_reflects_the_value_just_written(self) -> None:
        body = self._post("dash:settings_set", {"key": "mode", "value": '"auto"'}).content.decode()
        assert ">auto<" in body
        assert reverse("dash:settings_restore") in body  # now overridden → restore is offered

    def test_an_htmx_restore_answers_the_row_back_at_its_default(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        body = self._post("dash:settings_restore", {"key": "mode"}).content.decode()
        assert ConfigSetting.objects.get_effective("mode") is None
        assert body.lstrip().startswith("<tr")
        assert reverse("dash:settings_restore") not in body  # no row left to delete

    def test_a_secret_never_reaches_the_swapped_fragment(self) -> None:
        body = self._post("dash:settings_set", {"key": "banned_terms", "value": '["hushword"]'}).content.decode()
        assert ConfigSetting.objects.get_effective("banned_terms") == ["hushword"]
        assert "hushword" not in body
        assert "***" in body

    def test_a_refused_write_answers_the_row_carrying_the_reason(self) -> None:
        response = self._post("dash:settings_set", {"key": "mode", "value": "not-json"})
        body = response.content.decode()
        assert response.status_code == 400
        assert body.lstrip().startswith("<tr")
        assert "invalid JSON" in body
        assert ConfigSetting.objects.count() == 0

    def test_a_safety_posture_key_still_needs_the_confirm_phrase_over_htmx(self) -> None:
        response = self._post("dash:settings_set", {"key": "enforce_regulated_path", "value": "true"})
        assert response.status_code == 400
        assert SAFETY_CONFIRM_PHRASE in response.content.decode()
        assert ConfigSetting.objects.get_effective("enforce_regulated_path") is None

    def test_a_scoped_htmx_write_keeps_the_scope_on_the_swapped_row(self) -> None:
        body = self._post("dash:settings_set", {"key": "mode", "value": '"auto"', "scope": "proj"}).content.decode()
        assert ConfigSetting.objects.get_effective("mode", scope="proj") == "auto"
        assert 'name="scope" value="proj"' in body

    def test_a_plain_form_post_still_redirects_for_the_no_javascript_path(self) -> None:
        response = self.client.post(reverse("dash:settings_set"), {"key": "mode", "value": '"auto"'}, **_LOOPBACK)
        assert response.status_code == 302
        assert response["Location"] == reverse("dash:settings")


class TestSettingsExport(TestCase):
    def test_export_downloads_a_dump_withholding_secrets(self) -> None:
        ConfigSetting.objects.set_value("banned_brands", ["synthetic"])
        ConfigSetting.objects.set_value("mode", "auto")
        response = self.client.get(reverse("dash:settings_export"), **_LOOPBACK)
        body = response.content.decode()
        assert response.status_code == 200
        assert response["Content-Disposition"].startswith("attachment")
        assert "synthetic" not in body
        assert "mode" in body


class TestSettingsImport(TestCase):
    def test_dry_run_preview_writes_nothing(self) -> None:
        response = self.client.post(
            reverse("dash:settings_import"), {"toml": '[teatree]\nmode = "auto"\n', "apply": ""}, **_LOOPBACK
        )
        assert response.status_code == 200
        assert "dry-run" in response.content.decode()
        assert ConfigSetting.objects.count() == 0

    def test_apply_writes_the_rows(self) -> None:
        self.client.post(
            reverse("dash:settings_import"), {"toml": '[teatree]\nmode = "auto"\n', "apply": "1"}, **_LOOPBACK
        )
        assert ConfigSetting.objects.get_effective("mode") == "auto"

    def test_a_safety_posture_key_is_not_written_without_the_confirm_phrase(self) -> None:
        # The import textarea must not be the way around the confirm gate settings_set enforces.
        response = self.client.post(reverse("dash:settings_import"), {"toml": _SAFETY_TOML, "apply": "1"}, **_LOOPBACK)
        assert ConfigSetting.objects.get_effective("autonomy") is None
        assert "safety-posture" in response.content.decode()

    def test_a_safety_posture_key_is_written_with_the_confirm_phrase(self) -> None:
        self.client.post(
            reverse("dash:settings_import"),
            {"toml": _SAFETY_TOML, "apply": "1", "confirm": SAFETY_CONFIRM_PHRASE},
            **_LOOPBACK,
        )
        assert ConfigSetting.objects.get_effective("autonomy") == "full"

    def test_the_dry_run_preview_flags_the_safety_posture_row(self) -> None:
        response = self.client.post(reverse("dash:settings_import"), {"toml": _SAFETY_TOML, "apply": ""}, **_LOOPBACK)
        # Scoped to the import block — the editor table below it labels every safety key,
        # so an unscoped search would pass against a preview that flags nothing.
        block = _import_block(response.content.decode())
        assert "autonomy" in block
        assert "safety-posture" in block
        assert ConfigSetting.objects.count() == 0

    def test_a_non_safety_key_still_imports_with_no_confirm(self) -> None:
        self.client.post(
            reverse("dash:settings_import"), {"toml": '[teatree]\nmode = "auto"\n', "apply": "1"}, **_LOOPBACK
        )
        assert ConfigSetting.objects.get_effective("mode") == "auto"

    def test_apply_with_a_rejected_row_writes_nothing(self) -> None:
        response = self.client.post(
            reverse("dash:settings_import"),
            {"toml": '[teatree]\nnot_a_setting = 1\nmode = "auto"\n', "apply": "1"},
            **_LOOPBACK,
        )
        assert response.status_code == 200
        assert "rejected" in response.content.decode()
        assert ConfigSetting.objects.count() == 0


class TestShippedDefaultColumn(TestCase):
    """The page shows each key's shipped default and whether the effective value matches."""

    def _body(self) -> str:
        return self.client.get(reverse("dash:settings"), **_LOOPBACK).content.decode()

    def test_the_column_and_a_matching_verdict_render(self) -> None:
        body = self._body()
        assert "shipped default" in body
        assert "same as default" in body

    def test_an_override_renders_the_differing_verdict(self) -> None:
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 42)
        body = self._body()
        assert "differs from default" in body
        assert "default-differs" in body

    def test_the_verdict_is_never_colour_alone(self) -> None:
        # Each coloured span carries a text icon AND its own words, so the verdict survives
        # greyscale, colour blindness, and a screen reader.
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 42)
        body = self._body()
        for css_class, icon, words in (
            ("default-match", "&#10003;", "same as default"),
            ("default-differs", "&#9679;", "differs from default"),
        ):
            span = body[body.index(css_class) :][:200]
            assert icon in span, css_class
            assert words in span, css_class

    def test_a_secret_shipped_default_never_reaches_the_response_bytes(self) -> None:
        # The masking contract extends to the new column. Both default sources are forced
        # to carry a value here — the shipped table (which drives "has a default") and the
        # model accessor the column reads — so an unmasked column would serialise it.
        ConfigSetting.objects.set_value("banned_terms", ["supersecretcodename"])
        leaky = SimpleNamespace(**{**shipped_defaults().model_dump(), "banned_terms": ["anotherhushword"]})
        with (
            patch(
                "teatree.dash.settings_editor.shipped_defaults_table",
                return_value={**shipped_defaults_table(), "banned_terms": ["anotherhushword"]},
            ),
            patch("teatree.dash.settings_editor.shipped_defaults", return_value=leaky),
        ):
            body = self._body()
        assert "supersecretcodename" not in body
        assert "anotherhushword" not in body
        assert "banned_terms" in body


class SettingsScopeControlTestCase(TestCase):
    """The ``?scope=`` parameter is reachable from the UI, and an import keeps it.

    The view honoured ``?scope=`` and threaded it through every hidden input, but no
    control set it and ``settings_import`` always answered the global scope.

    The scope is real — the store is keyed ``(scope, key)`` and ``config_setting set
    --overlay`` writes it — so the honest fix is to surface it, not to delete it.
    """

    def test_the_page_offers_a_scope_picker(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "scoped", scope="demo-overlay")
        body = self.client.get(reverse("dash:settings")).content.decode()
        assert 'name="scope"' in body
        assert "demo-overlay" in body

    def test_the_picker_offers_the_global_scope_and_every_scope_holding_rows(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "a", scope="alpha")
        ConfigSetting.objects.set_value("issue_implementer_label", "b", scope="beta")
        editor = build_settings_editor()
        assert editor.available_scopes[0] == ""
        assert "alpha" in editor.available_scopes
        assert "beta" in editor.available_scopes

    def test_an_import_re_renders_the_scope_the_operator_was_editing(self) -> None:
        """The dump's own tables decide each row's scope; the PAGE scope decides the view.

        Answering the global editor after an overlay-scoped import silently moved the
        operator somewhere they did not navigate to.
        """
        ConfigSetting.objects.set_value("issue_implementer_label", "scoped", scope="demo-overlay")
        response = self.client.post(
            reverse("dash:settings_import"),
            {"toml": '[teatree]\nissue_implementer_label = "x"\n', "scope": "demo-overlay", "apply": ""},
        )
        assert response.status_code == 200
        assert response.context["editor"].scope == "demo-overlay"
