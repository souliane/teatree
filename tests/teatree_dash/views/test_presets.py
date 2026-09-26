"""The dashboard editor writes through the sanctioned seams and reads the shared resolver (#3559)."""

import datetime as dt
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse

from teatree.core.models import ConfigSetting, Loop, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.dash.preset_editor import build_preset_editor
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.enable_verdict import effective_verdicts


def _loop(name: str) -> Loop:
    loop, _ = Loop.objects.update_or_create(
        name=name,
        defaults={"script": f"src/teatree/loops/{name}/loop.py", "delay_seconds": 60, "enabled": None},
    )
    return loop


def _preset(name: str, entries: dict[str, bool] | None = None, **fields: object) -> Mode:
    preset, _ = Mode.objects.update_or_create(name=name, defaults={"entries": entries or {}, **fields})
    return preset


class PresetEntryPostTestCase(TestCase):
    """All three tri-state values round-trip through the UI, absence included."""

    def setUp(self) -> None:
        self.url = reverse("dash:preset_entry")
        _loop("review")
        _preset("present", {})
        ModeOverride.objects.set_override("present", reason="test override")
        self.addCleanup(ModeOverride.objects.clear)

    def _verdict(self, name: str) -> object:
        return next(verdict for verdict in effective_verdicts() if verdict.name == name)

    def _post(self, state: str) -> None:
        self.client.post(self.url, {"preset": "present", "loop": "review", "state": state})

    def test_setting_on_persists_and_the_resolver_agrees(self) -> None:
        self._post("on")
        assert Mode.objects.by_name("present").state_for("review") is True
        assert self._verdict("review").admitted is True

    def test_setting_off_persists_as_false(self) -> None:
        self._post("off")
        assert Mode.objects.by_name("present").state_for("review") is False

    def test_the_write_leaves_the_map_answering_for_every_loop(self) -> None:
        self._post("on")
        entries = Mode.objects.by_name("present").entries
        assert set(entries) == set(Loop.objects.values_list("name", flat=True))

    def test_inherit_is_no_longer_a_state(self) -> None:
        resp = self.client.post(self.url, {"preset": "present", "loop": "review", "state": "inherit"})
        assert resp.status_code == 400

    def test_unknown_state_is_rejected(self) -> None:
        resp = self.client.post(self.url, {"preset": "present", "loop": "review", "state": "maybe"})
        assert resp.status_code == 400

    def test_write_goes_through_the_service_seam(self) -> None:
        # Pinned at the seam so a future refactor to a raw row edit turns this RED.
        with mock.patch("teatree.dash.views.presets.set_preset_entry") as seam:
            self.client.post(self.url, {"preset": "present", "loop": "review", "state": "on"})
        seam.assert_called_once_with("present", "review", "on")

    def test_csrf_is_enforced(self) -> None:
        csrf_client = Client(enforce_csrf_checks=True)
        resp = csrf_client.post(self.url, {"preset": "present", "loop": "review", "state": "on"})
        assert resp.status_code == 403


class PresetUsePostTestCase(TestCase):
    def setUp(self) -> None:
        self.url = reverse("dash:preset_use")
        _preset("maintenance", {})
        self.addCleanup(ModeOverride.objects.clear)

    def test_activating_a_preset_persists_the_override(self) -> None:
        self.client.post(self.url, {"preset": "maintenance"})
        assert ModeOverride.objects.current().preset_name == "maintenance"

    def test_auto_clears_the_override(self) -> None:
        self.client.post(self.url, {"preset": "maintenance"})
        self.client.post(self.url, {"preset": "auto"})
        assert ModeOverride.objects.current() is None

    def test_unknown_preset_is_rejected(self) -> None:
        assert self.client.post(self.url, {"preset": "ghost"}).status_code == 400

    def test_activation_goes_through_the_service_seam(self) -> None:
        with mock.patch("teatree.dash.views.presets.activate_preset") as seam:
            self.client.post(self.url, {"preset": "maintenance"})
        assert seam.call_args.args == ("maintenance",)


class SchedulePostTestCase(TestCase):
    def setUp(self) -> None:
        self.schedule, _ = ModeSchedule.objects.get_or_create(name="dashsched")
        ModeScheduleSlot.objects.filter(schedule=self.schedule).delete()
        _preset("present", {})
        self.addCleanup(ConfigSetting.objects.clear, ACTIVE_SCHEDULE_SETTING)

    def test_switching_the_active_schedule_persists(self) -> None:
        self.client.post(reverse("dash:schedule_activate"), {"schedule": "dashsched"})
        assert ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING) == "dashsched"

    def test_clearing_the_active_schedule_persists(self) -> None:
        self.client.post(reverse("dash:schedule_activate"), {"schedule": "dashsched"})
        self.client.post(reverse("dash:schedule_activate"), {"schedule": "none"})
        assert ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING) in {None, ""}

    def test_adding_a_slot_persists(self) -> None:
        self.client.post(
            reverse("dash:schedule_slot"),
            {"schedule": "dashsched", "days": ["0", "4"], "start_time": "09:30", "preset": "present"},
        )
        slot = ModeScheduleSlot.objects.get(schedule=self.schedule)
        assert slot.weekdays == {0, 4}
        assert slot.start_time.strftime("%H:%M") == "09:30"

    def test_removing_a_slot_persists(self) -> None:
        slot = ModeScheduleSlot.objects.create(
            schedule=self.schedule, days=[0], start_time="09:30", preset_name="present"
        )
        self.client.post(reverse("dash:schedule_slot_delete"), {"schedule": "dashsched", "slot_id": slot.pk})
        assert not ModeScheduleSlot.objects.filter(pk=slot.pk).exists()

    def test_a_slot_with_no_days_is_rejected(self) -> None:
        resp = self.client.post(
            reverse("dash:schedule_slot"),
            {"schedule": "dashsched", "start_time": "09:30", "preset": "present"},
        )
        assert resp.status_code == 400


class PresetAdminPostTestCase(TestCase):
    def setUp(self) -> None:
        _preset("spare", {}, description="old text")
        self.addCleanup(ModeOverride.objects.clear)

    def test_creating_a_preset_persists(self) -> None:
        self.client.post(reverse("dash:preset_create"), {"name": "night-shift", "description": "Nights."})
        assert Mode.objects.by_name("night-shift").description == "Nights."

    def test_editing_the_description_persists(self) -> None:
        self.client.post(reverse("dash:preset_meta"), {"preset": "spare", "description": "new text"})
        assert Mode.objects.by_name("spare").description == "new text"

    def test_renaming_persists(self) -> None:
        self.client.post(reverse("dash:preset_rename"), {"preset": "spare", "new_name": "spare-tokens"})
        assert Mode.objects.by_name("spare") is None
        assert Mode.objects.by_name("spare-tokens") is not None

    def test_deleting_an_unreferenced_preset_persists(self) -> None:
        self.client.post(reverse("dash:preset_delete"), {"preset": "spare"})
        assert Mode.objects.by_name("spare") is None

    def test_deleting_the_active_preset_is_rejected(self) -> None:
        ModeOverride.objects.set_override("spare", reason="test override")
        resp = self.client.post(reverse("dash:preset_delete"), {"preset": "spare"})
        assert resp.status_code == 400
        assert Mode.objects.by_name("spare") is not None


class PresetEditorPageTestCase(TestCase):
    def setUp(self) -> None:
        _loop("inbox")
        _preset("present", {"inbox": True})

    def test_page_renders_the_preset_tab(self) -> None:
        resp = self.client.get(reverse("dash:presets"), {"preset": "present"})
        assert resp.status_code == 200
        assert b"present" in resp.content

    def test_the_page_says_a_preset_answers_for_every_loop(self) -> None:
        _loop("review")
        resp = self.client.get(reverse("dash:presets"), {"preset": "present"})
        assert b"answers for every loop" in resp.content

    def test_the_two_tallies_account_for_every_row(self) -> None:
        _loop("review")
        card = build_preset_editor(selected="present").selected_card
        assert card is not None
        assert card.on_count + card.off_count == len(card.entries)


class PresetsHtmxSwapTestCase(TestCase):
    """Every preset/schedule POST answers the page body — never a full-document redirect."""

    def _post(self, name: str, data: dict[str, str], *, htmx: bool = True) -> object:
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        return self.client.post(reverse(name), data, **headers)

    def test_every_mutating_form_on_the_page_is_wired_to_swap(self) -> None:
        Mode.objects.get_or_create(name="present", defaults={"entries": {}})
        schedule, _ = ModeSchedule.objects.get_or_create(name="weekly")
        ModeScheduleSlot.objects.create(schedule=schedule, days=[0], start_time=dt.time(9, 0), preset_name="present")
        body = self.client.get(reverse("dash:presets")).content.decode()
        posts = (
            "dash:preset_use",
            "dash:preset_create",
            "dash:preset_meta",
            "dash:preset_rename",
            "dash:preset_delete",
            "dash:preset_entry",
            "dash:schedule_activate",
            "dash:schedule_slot",
            "dash:schedule_slot_delete",
        )
        for action in posts:
            assert f'hx-post="{reverse(action)}"' in body, f"{action} form is not wired to an htmx swap"

    def test_an_htmx_create_answers_the_body_fragment(self) -> None:
        response = self._post("dash:preset_create", {"name": "fresh", "description": "x"})
        assert response.status_code == 200
        body = response.content.decode()
        assert "<!doctype html>" not in body.lower()
        assert "fresh" in body

    def test_a_no_js_create_keeps_the_redirect(self) -> None:
        assert self._post("dash:preset_create", {"name": "plain", "description": ""}, htmx=False).status_code == 302

    def test_a_refused_write_answers_the_body_with_its_reason(self) -> None:
        response = self._post("dash:preset_create", {"name": "not a slug!", "description": ""})
        assert response.status_code == 400
        assert "invalid preset name" in response.content.decode()

    def test_a_no_js_refusal_renders_a_page_with_navigation(self) -> None:
        response = self._post("dash:preset_create", {"name": "not a slug!", "description": ""}, htmx=False)
        assert response.status_code == 400
        body = response.content.decode()
        assert "<!doctype html>" in body.lower()
        assert reverse("dash:presets") in body
