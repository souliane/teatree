"""Canonical dedup key + state-hash helpers for self-improve detectors.

Each detector defines a deterministic ``dedup_key`` (per BLUEPRINT § 5.7)
that anchors one firing identity, plus a ``state_hash`` over the
underlying evidence so a re-fire on the same key but different evidence
escalates one rung rather than being suppressed by cool-down.

The canonical-key builder joins a detector-stable prefix with the
identity fragment using a delimiter that cannot appear in a URL or PK,
so a future detector tag rename never collides with a real firing.
"""

import hashlib

_KEY_DELIMITER = "::"


def canonical_key(detector_tag: str, identity: str) -> str:
    """Compose a stable ``<detector>::<identity>`` dedup key."""
    return f"{detector_tag}{_KEY_DELIMITER}{identity}"


def state_hash(*parts: object) -> str:
    """Stable SHA-256 over the detector's observed evidence parts.

    ``None`` parts are normalised to the empty string so an absent
    optional field never changes the hash by accident.
    """
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
