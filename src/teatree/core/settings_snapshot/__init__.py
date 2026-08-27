"""One teatree instance's settings, loops, presets and schedules as comparable JSON.

The payload another instance fetches to diff itself against this one — served read-only by
``GET /dash/settings/snapshot.json``. It carries no raw secret by construction (there is no
private mode here), so exposing it over a loopback tunnel exposes no value.
"""

from teatree.core.settings_snapshot.fingerprint import SnapshotError, Warnings, schema_state
from teatree.core.settings_snapshot.payload import FORMAT_VERSION, SNAPSHOT_FORMAT, build_snapshot
from teatree.core.settings_snapshot.serialisation import canonical_json, digest, redaction_stub, serialise

__all__ = [
    "FORMAT_VERSION",
    "SNAPSHOT_FORMAT",
    "SnapshotError",
    "Warnings",
    "build_snapshot",
    "canonical_json",
    "digest",
    "redaction_stub",
    "schema_state",
    "serialise",
]
