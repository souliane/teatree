"""Does the term registry a gate loaded match the digests committed in ``.github/term-source-fingerprint.json``?

The pre-push leak gate reads its registry from the environment (``TEATREE_TERM_REGISTRY``, the config
DB, ``XDG_DATA_HOME``, ``HOME``), and a hook cannot tell an inherited fake from the owner's own store.
What it can do is compare the lists it LOADED with the digests the repository commits for them, which
the required ``term-source-drift`` CI job holds equal to the real secret. Only digests and counts are
read or reported, never a term.

The digest is the one ``scripts/term_source_drift.py`` writes; that script runs on a bare CI runner with
no ``teatree`` import, so the two keep their own copy and ``tests/teatree_hooks/test_term_fingerprint.py``
holds them equal.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

__all__ = ["FingerprintUnreadableError", "gate_fingerprints", "unverified_gates"]

_DIGEST_DOMAIN: Final = b"teatree-term-source-fingerprint-v1\n"

#: The registry class each CI-fed gate scans; the other classes have no committed fingerprint.
GATE_CLASSES: Final[dict[str, str]] = {"tree": "leak", "overlay": "overlay"}


class FingerprintUnreadableError(Exception):
    """The committed fingerprint is missing, unreadable or malformed: nothing to compare with, so no match."""


def _normalised(terms: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({term for term in (str(raw).strip().casefold() for raw in terms) if term}))


def _digest(terms: tuple[str, ...]) -> str:
    payload = _DIGEST_DOMAIN + "\n".join(terms).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def gate_fingerprints(registry: Mapping[str, Sequence[str]]) -> dict[str, dict[str, int | str]]:
    """``{gate: {count, digest}}`` of the lists *registry* holds for the two CI-fed gates."""
    fingerprints: dict[str, dict[str, int | str]] = {}
    for gate, term_class in GATE_CLASSES.items():
        terms = _normalised(registry.get(term_class, ()))
        fingerprints[gate] = {"count": len(terms), "digest": _digest(terms)}
    return fingerprints


def _committed_gates(path: Path) -> Mapping[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"the committed term-source fingerprint {path.name} cannot be read ({type(exc).__name__})"
        raise FingerprintUnreadableError(msg) from exc
    gates = document.get("gates") if isinstance(document, dict) else None
    if not isinstance(gates, dict) or not all(isinstance(gates.get(gate), dict) for gate in GATE_CLASSES):
        msg = f"the committed term-source fingerprint {path.name} does not carry both gates"
        raise FingerprintUnreadableError(msg)
    return gates


def unverified_gates(registry: Mapping[str, Sequence[str]], fingerprint_file: Path) -> list[str]:
    """The gates whose list in *registry* differs from the one *fingerprint_file* commits, by name only."""
    committed = _committed_gates(fingerprint_file)
    loaded = gate_fingerprints(registry)
    unverified: list[str] = []
    for gate in GATE_CLASSES:
        entry = committed[gate]
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("digest"), str)
            or not isinstance(entry.get("count"), int)
        ):
            msg = f"the committed term-source fingerprint {fingerprint_file.name} has a malformed {gate!r} entry"
            raise FingerprintUnreadableError(msg)
        if entry["digest"] != loaded[gate]["digest"] or entry["count"] != loaded[gate]["count"]:
            unverified.append(gate)
    return unverified
