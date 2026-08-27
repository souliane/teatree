"""The settings snapshot: the payload's shape, and the two promises it must keep.

The promises, in order of how badly a regression would hurt:

1.  **No raw secret leaves the box.** There is no private mode, so a stored secret is present
    only as its reason plus a digest — over the WHOLE serialised payload, not one field.
2.  **The comparability stamp is honest.** The settings half hard-fails rather than emitting a
    partial one; the schema half leaves a failed source OUT rather than filling it with an
    empty digest two failing boxes would agree on.
"""

import json

from django.test import TestCase

from teatree.config import ALL_KNOWN_CONFIG_SETTINGS
from teatree.core.config_interchange.seed_tables import seed_models
from teatree.core.models import ConfigSetting, Mode
from teatree.core.settings_snapshot import SNAPSHOT_FORMAT, build_snapshot, canonical_json, redaction_stub, serialise
from teatree.core.settings_snapshot.fingerprint import Warnings, schema_state

_SECRET_VALUE = "sk-live-nobody-should-ever-see-this"
#: Credential COORDINATES — they name where a secret lives, and are withheld for that alone.
_FAKE_COORDINATE = "fake-store/not-a-real-entry"
_OTHER_FAKE_COORDINATE = "fake-store/also-not-a-real-entry"


class TestPayloadShape(TestCase):
    def test_the_format_stamp_is_the_one_the_offline_page_reads(self) -> None:
        snapshot = build_snapshot("box-a")
        assert snapshot["format"] == SNAPSHOT_FORMAT
        assert snapshot["format_version"] == 1

    def test_the_top_level_keys_are_the_documented_set(self) -> None:
        assert sorted(build_snapshot("box-a")) == [
            "capture_warnings",
            "captured_at",
            "export_toml",
            "fingerprint",
            "format",
            "format_version",
            "includes_private",
            "instance",
            "omitted",
            "redacted",
            "registry",
            "values",
        ]

    def test_the_label_and_note_ride_into_the_instance_block(self) -> None:
        instance = build_snapshot("box-a", "the laptop")["instance"]
        assert instance["label"] == "box-a"
        assert instance["note"] == "the laptop"

    def test_every_known_setting_key_is_declared_in_the_registry(self) -> None:
        registry = build_snapshot("box-a")["registry"]["settings"]
        assert sorted(registry) == sorted(ALL_KNOWN_CONFIG_SETTINGS)

    def test_every_registry_entry_carries_the_fields_a_comparison_reads(self) -> None:
        entry = build_snapshot("box-a")["registry"]["settings"]["merge_wip"]
        assert sorted(entry) == [
            "annotation",
            "category",
            "choices",
            "default",
            "feature_flag",
            "group",
            "help",
            "home",
            "kind",
            "registry",
            "safety_posture",
            "sync_note",
            "syncable",
        ]

    def test_every_seed_family_is_present_with_its_field_registry(self) -> None:
        seed = build_snapshot("box-a")["registry"]["seed"]
        assert sorted(seed) == sorted(seed_models())
        assert all("fields" in entry for entry in seed.values())

    def test_the_whole_payload_is_json_serialisable(self) -> None:
        assert json.loads(json.dumps(build_snapshot("box-a")))


class TestNoRawSecretLeaves(TestCase):
    def _snapshot_with_a_stored_secret(self) -> dict:
        registry = build_snapshot("box-a")["registry"]["settings"]
        key = next(
            key
            for key in sorted(ALL_KNOWN_CONFIG_SETTINGS)
            if registry[key]["syncable"] is False and registry[key]["kind"] == "str"
        )
        ConfigSetting.objects.update_or_create(key=key, scope="", defaults={"value": _SECRET_VALUE})
        return build_snapshot("box-a")

    def test_the_payload_declares_it_carries_no_private_values(self) -> None:
        assert build_snapshot("box-a")["includes_private"] is False

    def test_a_stored_secret_value_appears_nowhere_in_the_serialised_payload(self) -> None:
        assert _SECRET_VALUE not in json.dumps(self._snapshot_with_a_stored_secret())

    def test_a_withheld_row_is_reported_rather_than_silently_dropped(self) -> None:
        snapshot = self._snapshot_with_a_stored_secret()
        assert snapshot["redacted"], "a withheld row must be named so the reader knows it exists"
        assert all({"scope", "key", "path", "reason"} == set(row) for row in snapshot["redacted"])

    def test_a_withheld_value_still_carries_a_digest_so_boxes_can_be_compared(self) -> None:
        snapshot = self._snapshot_with_a_stored_secret()
        withheld = snapshot["redacted"][0]
        stub = snapshot["values"]["settings"][withheld["scope"]][withheld["key"]]
        assert stub["__redacted__"] == withheld["reason"]
        assert stub["sha256"] == redaction_stub("", _SECRET_VALUE)["sha256"]

    def test_a_withheld_key_is_marked_unsyncable_whatever_its_declaration_said(self) -> None:
        snapshot = self._snapshot_with_a_stored_secret()
        withheld = snapshot["redacted"][0]["key"]
        assert snapshot["registry"]["settings"][withheld]["syncable"] is False


class TestACredentialInsideAnInnocuousRowIsStillWithheld(TestCase):
    """The negative direction: the row's KEY says nothing, and the coordinate rides in its VALUE.

    ``overlays`` is a registry row whose value is a table of overlay definitions, and an
    overlay definition names where its Slack credential lives. A withhold rule that only reads
    the row key cannot see that, so the coordinate rode into a payload built to be SHARED.
    """

    def _snapshot_with_nested_coordinates(self) -> dict:
        ConfigSetting.objects.update_or_create(
            key="overlays",
            scope="",
            defaults={
                "value": {
                    "box": {
                        "messaging_backend": "slack",
                        # named by no list: caught by the `*_pass_key` suffix rule alone
                        "gitlab_token_pass_key": _FAKE_COORDINATE,
                        "slack_token_ref": _OTHER_FAKE_COORDINATE,
                    }
                }
            },
        )
        return build_snapshot("box-a")

    def _stored_overlays(self) -> dict:
        return self._snapshot_with_nested_coordinates()["values"]["settings"][""]["overlays"]["box"]

    def test_no_nested_coordinate_appears_anywhere_in_the_serialised_payload(self) -> None:
        payload = json.dumps(self._snapshot_with_nested_coordinates())
        assert _FAKE_COORDINATE not in payload
        assert _OTHER_FAKE_COORDINATE not in payload

    def test_a_coordinate_no_list_names_is_caught_by_the_suffix_rule_alone(self) -> None:
        assert self._stored_overlays()["gitlab_token_pass_key"]["__redacted__"] == "credential-coordinate"

    def test_only_the_withheld_leaves_are_replaced_so_the_rest_still_compares(self) -> None:
        stored = self._stored_overlays()
        assert stored["messaging_backend"] == "slack"
        assert stored["slack_token_ref"]["__redacted__"] == "private-key"

    def test_each_stub_keeps_the_digest_that_lets_two_boxes_still_be_compared(self) -> None:
        stored = self._stored_overlays()
        assert stored["gitlab_token_pass_key"]["sha256"] == redaction_stub("", _FAKE_COORDINATE)["sha256"]

    def test_every_withheld_leaf_is_reported_with_the_path_it_sat_at(self) -> None:
        notices = self._snapshot_with_nested_coordinates()["redacted"]
        assert [(row["path"], row["reason"]) for row in notices if row["key"] == "overlays"] == [
            ("box.gitlab_token_pass_key", "credential-coordinate"),
            ("box.slack_token_ref", "private-key"),
        ]

    def test_the_row_carrying_them_can_never_ride_an_import(self) -> None:
        snapshot = self._snapshot_with_nested_coordinates()
        entry = snapshot["registry"]["settings"]["overlays"]
        assert entry["syncable"] is False
        assert "box.gitlab_token_pass_key" in entry["sync_note"]


class TestASeedRowCarriesNoCoordinateEither(TestCase):
    """The seed half of the same class: a preset's ``entries`` is a table of setting values."""

    def _snapshot_with_a_preset_holding_a_coordinate(self) -> dict:
        Mode.objects.create(name="withheld-probe", entries={"gitlab_token_pass_key": _FAKE_COORDINATE})
        return build_snapshot("box-a")

    def test_the_coordinate_appears_nowhere_in_the_serialised_payload(self) -> None:
        assert _FAKE_COORDINATE not in json.dumps(self._snapshot_with_a_preset_holding_a_coordinate())

    def test_only_the_withheld_entry_is_replaced(self) -> None:
        entries = self._snapshot_with_a_preset_holding_a_coordinate()["values"]["seed"]["modes"]["withheld-probe"][
            "entries"
        ]
        assert entries["gitlab_token_pass_key"]["__redacted__"] == "credential-coordinate"


class TestFingerprint(TestCase):
    def test_the_settings_half_is_always_present_because_it_may_not_fail_soft(self) -> None:
        fingerprint = build_snapshot("box-a")["fingerprint"]
        assert fingerprint["settings_schema_sha256"]
        assert fingerprint["settings_key_count"] == len(ALL_KNOWN_CONFIG_SETTINGS)
        assert fingerprint["defaults_toml_sha256"]
        assert fingerprint["seed_fields_sha256"]

    def test_two_captures_of_one_unchanged_box_agree_on_every_digest(self) -> None:
        first, second = build_snapshot("box-a")["fingerprint"], build_snapshot("box-b")["fingerprint"]
        assert first["settings_schema_sha256"] == second["settings_schema_sha256"]
        assert first["physical_schema_sha256"] == second["physical_schema_sha256"]

    def test_the_schema_half_reads_the_columns_rather_than_the_migration_history(self) -> None:
        state = schema_state(Warnings())
        assert state["table_count"] == len(state["table_shapes"])
        assert state["physical_schema_sha256"]

    def test_a_failed_optional_source_is_left_out_rather_than_emitted_empty(self) -> None:
        warn = Warnings()
        assert warn.optional("a source that raises", _raises, None) is None
        assert warn.messages, "a degraded source must be recorded, never swallowed"

    def test_a_degraded_source_records_its_type_and_never_the_exception_text(self) -> None:
        # A coercion error quotes the value it refused, and this log is SERVED to a peer.
        warn = Warnings()
        warn.optional("a source that raises", _raises, None)
        assert "RuntimeError" in warn.messages[0]
        assert _SECRET_VALUE not in warn.messages[0]


class TestSerialisation(TestCase):
    def test_canonical_json_is_key_order_independent(self) -> None:
        assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})

    def test_a_set_serialises_in_a_stable_order(self) -> None:
        assert serialise({"b", "a"}) == serialise({"a", "b"})

    def test_a_redaction_stub_never_carries_the_value(self) -> None:
        stub = redaction_stub("secret", _SECRET_VALUE)
        assert _SECRET_VALUE not in json.dumps(stub)
        assert stub["__redacted__"] == "secret"


def _raises() -> None:
    message = f"this source refused {_SECRET_VALUE!r}"
    raise RuntimeError(message)
