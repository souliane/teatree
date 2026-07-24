"""The dashboard editor writes through the sanctioned seams and reads the shared resolver (#3559)."""

from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse

from teatree.core.models import ConfigSetting, Loop, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.dash.preset_editor import build_preset_editor
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.preset_status import effective_verdicts


def _loop(name: str, *, enabled: bool = True) -> Loop:
    loop, _ = Loop.objects.update_or_create(
        name=name,
        defaults={"script": f"src/teatree/loops/{name}/loop.py", "delay_seconds": 60, "enabled": enabled},
    )
    return loop


def _preset(name: str, entries: dict[str, bool] | None = None, **fields: object) -> Mode:
    preset, _ = Mode.objects.update_or_create(name=name, defaults={"entries": entries or {}, **fields})
    return preset


class PresetEntryPostTestCase(TestCase):
    """All three tri-state values round-trip through the UI, absence included."""

    def setUp(self) -> None:
        self.url = reverse("dash:preset_entry")
        _loop("review", enabled=False)
        _preset("engaged", {})
        ModeOverride.objects.set_override("engaged")
        self.addCleanup(ModeOverride.objects.clear)

    def _verdict(self, name: str) -> object:
        return next(verdict for verdict in effective_verdicts() if verdict.name == name)

    def _post(self, state: str) -> None:
        self.client.post(self.url, {"preset": "engaged", "loop": "review", "state": state})

    def test_setting_on_persists_and_the_resolver_agrees(self) -> None:
        self._post("on")
        assert Mode.objects.by_name("engaged").entries == {"review": True}
        assert self._verdict("review").admitted is True

    def test_setting_off_persists_as_false(self) -> None:
        self._post("off")
        assert Mode.objects.by_name("engaged").entries == {"review": False}

    def test_setting_inherit_stores_no_entry_at_all(self) -> None:
        self._post("on")
        self._post("inherit")
        entries = Mode.objects.by_name("engaged").entries
        assert "review" not in entries
        assert entries.get("review") is not False

    def test_returning_to_inherit_hands_the_decision_back_to_the_base(self) -> None:
        self._post("on")
        self._post("inherit")
        verdict = self._verdict("review")
        assert verdict.layer == "base"
        assert verdict.admitted is False

    def test_unknown_state_is_rejected(self) -> None:
        resp = self.client.post(self.url, {"preset": "engaged", "loop": "review", "state": "maybe"})
        assert resp.status_code == 400

    def test_write_goes_through_the_service_seam(self) -> None:
        # Pinned at the seam so a future refactor to a raw row edit turns this RED.
        with mock.patch("teatree.dash.views.presets.set_preset_entry") as seam:
            self.client.post(self.url, {"preset": "engaged", "loop": "review", "state": "on"})
        seam.assert_called_once_with("engaged", "review", "on")

    def test_csrf_is_enforced(self) -> None:
        csrf_client = Client(enforce_csrf_checks=True)
        resp = csrf_client.post(self.url, {"preset": "engaged", "loop": "review", "state": "on"})
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
        _preset("engaged", {})
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
            {"schedule": "dashsched", "days": ["0", "4"], "start_time": "09:30", "preset": "engaged"},
        )
        slot = ModeScheduleSlot.objects.get(schedule=self.schedule)
        assert slot.weekdays == {0, 4}
        assert slot.start_time.strftime("%H:%M") == "09:30"

    def test_removing_a_slot_persists(self) -> None:
        slot = ModeScheduleSlot.objects.create(
            schedule=self.schedule, days=[0], start_time="09:30", preset_name="engaged"
        )
        self.client.post(reverse("dash:schedule_slot_delete"), {"schedule": "dashsched", "slot_id": slot.pk})
        assert not ModeScheduleSlot.objects.filter(pk=slot.pk).exists()

    def test_a_slot_with_no_days_is_rejected(self) -> None:
        resp = self.client.post(
            reverse("dash:schedule_slot"),
            {"schedule": "dashsched", "start_time": "09:30", "preset": "engaged"},
        )
        assert resp.status_code == 400


class PresetAdminPostTestCase(TestCase):
    def setUp(self) -> None:
        _preset("spare", {}, description="old text", availability_mode="away")
        self.addCleanup(ModeOverride.objects.clear)

    def test_creating_a_preset_persists(self) -> None:
        self.client.post(reverse("dash:preset_create"), {"name": "night-shift", "description": "Nights."})
        assert Mode.objects.by_name("night-shift").description == "Nights."

    def test_editing_description_and_clearing_the_pin_persists(self) -> None:
        self.client.post(
            reverse("dash:preset_meta"), {"preset": "spare", "description": "new text", "availability_pin": ""}
        )
        preset = Mode.objects.by_name("spare")
        assert preset.description == "new text"
        assert preset.availability_pin is None

    def test_renaming_persists(self) -> None:
        self.client.post(reverse("dash:preset_rename"), {"preset": "spare", "new_name": "spare-tokens"})
        assert Mode.objects.by_name("spare") is None
        assert Mode.objects.by_name("spare-tokens") is not None

    def test_deleting_an_unreferenced_preset_persists(self) -> None:
        self.client.post(reverse("dash:preset_delete"), {"preset": "spare"})
        assert Mode.objects.by_name("spare") is None

    def test_deleting_the_active_preset_is_rejected(self) -> None:
        ModeOverride.objects.set_override("spare")
        resp = self.client.post(reverse("dash:preset_delete"), {"preset": "spare"})
        assert resp.status_code == 400
        assert Mode.objects.by_name("spare") is not None


class PresetEditorPageTestCase(TestCase):
    def setUp(self) -> None:
        _loop("inbox", enabled=True)
        _preset("engaged", {"inbox": True})

    def test_page_renders_the_preset_tab(self) -> None:
        resp = self.client.get(reverse("dash:presets"), {"preset": "engaged"})
        assert resp.status_code == 200
        assert b"engaged" in resp.content

    def test_page_surfaces_the_no_opinion_wording(self) -> None:
        _loop("review")
        resp = self.client.get(reverse("dash:presets"), {"preset": "engaged"})
        assert b"No opinion on" in resp.content

    def test_read_model_names_the_loops_the_preset_leaves_undecided(self) -> None:
        _loop("review")
        card = next(c for c in build_preset_editor(selected="engaged").presets if c.name == "engaged")
        assert "review" in card.inherit_loops
        assert "inbox" not in card.inherit_loops
