"""The import/export page — its own surface, stating a scope wider than the settings page.

The control used to sit on the settings page while acting on a superset of it (#4340), so
what is asserted here is not only that export and import still work but that the page SAYS
what they reach: every documented section named up front, and a preview counted per family.
"""

import io

from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from teatree.core.config_interchange.migration import export_db_to_toml
from teatree.core.config_interchange.scope import EXPORT_SECTIONS
from teatree.core.models import ConfigSetting, Loop
from teatree.dash.views.base import SAFETY_CONFIRM_PHRASE
from teatree.dash.views.interchange import MAX_IMPORT_BYTES

_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}
_SAFETY_TOML = '[teatree]\nautonomy = "babysit"\n'
_TUNED_LOOP = "housekeeping"


def _upload(text: str, name: str = "config.toml") -> io.BytesIO:
    upload = io.BytesIO(text.encode("utf-8"))
    upload.name = name
    return upload


def _import_block(body: str) -> str:
    """The import-result region only — the scope band above it names the same words."""
    start = body.index('class="import-result"')
    return body[start : body.index("</section>", start)]


class TestThePageStatesItsScope(TestCase):
    def _body(self) -> str:
        return self.client.get(reverse("dash:interchange"), **_LOOPBACK).content.decode()

    def test_the_page_renders_and_is_in_the_nav(self) -> None:
        response = self.client.get(reverse("dash:interchange"), **_LOOPBACK)
        assert response.status_code == 200
        assert "dash:interchange" in [name for name, _ in response.context["nav_items"]]

    def test_every_documented_section_is_named_on_the_page(self) -> None:
        body = self._body()
        for section in EXPORT_SECTIONS:
            assert section.label in body, section.table
            assert escape(section.covers) in body, section.table

    def test_the_page_names_the_families_beyond_the_settings_store(self) -> None:
        body = self._body()
        for table in ("loops", "modes", "schedules"):
            assert table in body, table

    def test_the_settings_page_points_here_instead_of_hosting_the_forms(self) -> None:
        body = self.client.get(reverse("dash:settings"), **_LOOPBACK).content.decode()
        assert reverse("dash:interchange") in body
        assert 'name="toml_file"' not in body
        assert 'name="default_keys_only"' not in body


class TestExport(TestCase):
    def test_export_downloads_a_dump_withholding_secrets(self) -> None:
        ConfigSetting.objects.set_value("banned_brands", ["synthetic"])
        ConfigSetting.objects.set_value("mode", "auto")
        response = self.client.get(reverse("dash:interchange_export"), **_LOOPBACK)
        body = response.content.decode()
        assert response.status_code == 200
        assert response["Content-Disposition"] == 'attachment; filename="teatree-config.toml"'
        assert "synthetic" not in body
        assert "mode" in body

    def test_the_page_offers_both_filters_unticked(self) -> None:
        body = self.client.get(reverse("dash:interchange"), **_LOOPBACK).content.decode()
        for name in ("default_keys_only", "include_defaults"):
            control = body[body.index(f'name="{name}"') - 60 : body.index(f'name="{name}"') + 60]
            assert 'type="checkbox"' in control, name
            assert "checked" not in control, name

    def test_both_filters_download_the_defaults_shape_under_its_own_name(self) -> None:
        url = f"{reverse('dash:interchange_export')}?default_keys_only=1&include_defaults=1"
        response = self.client.get(url, **_LOOPBACK)
        body = response.content.decode()
        assert response["Content-Disposition"] == 'attachment; filename="defaults.toml"'
        assert body.startswith("# teatree shipped defaults")
        assert "merge_wip" in body

    def test_the_retired_settings_route_still_downloads_with_its_filters(self) -> None:
        url = f"{reverse('dash:settings_export')}?default_keys_only=1&include_defaults=1"
        response = self.client.get(url, follow=True, **_LOOPBACK)
        assert response.redirect_chain[0][0].startswith(reverse("dash:interchange_export"))
        assert response["Content-Disposition"] == 'attachment; filename="defaults.toml"'


class TestImport(TestCase):
    """Import is a file upload — the operator picks the file they exported."""

    def _post(self, text: str, **extra: str):
        return self.client.post(reverse("dash:interchange_import"), {"toml_file": _upload(text), **extra}, **_LOOPBACK)

    def test_the_page_offers_a_file_input_not_a_textarea(self) -> None:
        body = self.client.get(reverse("dash:interchange"), **_LOOPBACK).content.decode()
        assert 'type="file"' in body
        assert 'name="toml_file"' in body
        assert 'enctype="multipart/form-data"' in body
        assert "<textarea" not in body

    def test_the_upload_form_carries_the_one_csrf_token_the_page_needs(self) -> None:
        # The upload needs a real form field — the htmx pages ride the body's hx-headers.
        body = self.client.get(reverse("dash:interchange"), **_LOOPBACK).content.decode()
        assert body.count("csrfmiddlewaretoken") == 1

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
        response = self.client.post(reverse("dash:interchange_import"), {"apply": ""}, **_LOOPBACK)
        assert response.status_code == 400
        assert "choose a .toml file" in response.content.decode()

    def test_a_non_utf8_file_is_refused_with_a_reason(self) -> None:
        upload = io.BytesIO(b"\xff\xfe\x00binary")
        upload.name = "config.toml"
        response = self.client.post(reverse("dash:interchange_import"), {"toml_file": upload}, **_LOOPBACK)
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
        body = self._post(_SAFETY_TOML, apply="").content.decode()
        assert "autonomy" in body
        assert "safety-posture" in body
        assert ConfigSetting.objects.count() == 0

    def test_apply_with_a_rejected_row_writes_nothing(self) -> None:
        response = self._post('[teatree]\nnot_a_setting = 1\nmode = "auto"\n', apply="1")
        assert response.status_code == 200
        assert "rejected" in response.content.decode()
        assert ConfigSetting.objects.count() == 0

    def test_a_personal_backup_upload_is_told_where_it_can_be_restored(self) -> None:
        # The page cannot restore private rows, so a refusal that only listed per-key secret
        # rejections would leave the operator with a file and no next step (#4156).
        ConfigSetting.objects.set_value("slack_user_id", "<the-operator>")
        body = self._post(export_db_to_toml(include_private=True, scan_terms=()).toml).content.decode()
        assert "--restore-private" in _import_block(body)

    def test_an_ordinary_rejected_upload_is_not(self) -> None:
        # Anti-vacuous control: the pointer is tied to the backup marker, not to any refusal.
        body = self._post("[teatree]\nnot_a_setting = 1\n").content.decode()
        assert "--restore-private" not in _import_block(body)


class TestThePreviewShowsTheBreadth(TestCase):
    """A preview that reads as "12 rows" hides that one of them changes which loops run."""

    def _post(self, text: str, **extra: str):
        return self.client.post(reverse("dash:interchange_import"), {"toml_file": _upload(text), **extra}, **_LOOPBACK)

    def test_a_loop_row_is_previewed_under_the_loops_section(self) -> None:
        body = self._post(f"[loops.{_TUNED_LOOP}]\ndelay_seconds = 4242\n", apply="").content.decode()
        assert "Loops" in body
        assert Loop.objects.get(name=_TUNED_LOOP).delay_seconds != 4242

    def test_a_settings_only_file_does_not_claim_to_touch_the_loops(self) -> None:
        response = self._post('[teatree]\nmode = "interactive"\n', apply="")
        labels = [change.section.label for change in response.context["changed_sections"]]
        assert labels == ["Config settings"]

    def test_a_mixed_file_names_both_sections(self) -> None:
        text = f'[teatree]\nmode = "interactive"\n\n[loops.{_TUNED_LOOP}]\ndelay_seconds = 4242\n'
        response = self._post(text, apply="")
        labels = [change.section.label for change in response.context["changed_sections"]]
        assert labels == ["Config settings", "Loops"]
