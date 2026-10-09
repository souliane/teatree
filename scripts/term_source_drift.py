#!/usr/bin/env python3
"""Compare the CI registry secret against committed term fingerprints.

The tree and overlay gates consume classes from one ``TEATREE_TERM_REGISTRY``
JSON secret. Reports contain only counts and whole-list digests.
"""

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_FINGERPRINT: Final = REPO_ROOT / ".github" / "term-source-fingerprint.json"

#: Domain separation, so the digest is specific to this contract rather than a
#: bare hash of the list any other tool might also produce.
_DIGEST_DOMAIN: Final = b"teatree-term-source-fingerprint-v1\n"

_DOCUMENT_NOTE: Final = (
    "Counts and salted digests only — never term values. Regenerate with "
    "`python scripts/term_source_drift.py sync --apply`, which updates the "
    "repository secrets and this file together."
)

OK: Final = 0
DRIFT: Final = 1
MISCONFIGURED: Final = 2


def normalise_terms(terms: object) -> tuple[str, ...]:
    """Sorted, de-duplicated, case- and whitespace-insensitive form of *terms*.

    Reordering a secret or changing a term's case is not drift, so normalisation
    happens before both the count and the digest.
    """
    if not isinstance(terms, list | tuple):
        return ()
    cleaned = {str(term).strip().casefold() for term in terms}
    return tuple(sorted(term for term in cleaned if term))


def terms_from_env(env_var: str, term_class: str) -> tuple[str, ...] | None:
    """One class from the JSON registry secret, or ``None`` when it is unset.

    ``None`` means the required CI secret is unset, rather than an empty list
    the operator chose. Fork PRs skip this scan in the workflow before it runs.
    """
    raw = os.environ.get(env_var, "")
    if not raw.strip():
        return None
    try:
        registry = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"${env_var} is not JSON"
        raise ValueError(msg) from exc
    if not isinstance(registry, dict) or not isinstance(registry.get(term_class), list):
        msg = f"${env_var} has no {term_class!r} list"
        raise TypeError(msg)
    return normalise_terms(registry[term_class])


@dataclass(frozen=True)
class Fingerprint:
    """A term list reduced to what is safe to publish: its size and its digest."""

    count: int
    digest: str

    @classmethod
    def of(cls, terms: tuple[str, ...]) -> "Fingerprint":
        """Fingerprint an already-normalised list."""
        payload = _DIGEST_DOMAIN + "\n".join(terms).encode("utf-8")
        return cls(count=len(terms), digest=f"sha256:{hashlib.sha256(payload).hexdigest()}")

    @classmethod
    def from_stored(cls, raw: object) -> "Fingerprint":
        """Rebuild a fingerprint from its committed JSON form, failing loud when malformed.

        A malformed entry must raise rather than read as an empty contract that
        every list would satisfy.
        """
        if not isinstance(raw, dict):
            msg = f"expected a fingerprint table, got {type(raw).__name__}"
            raise TypeError(msg)
        fields = {str(key): value for key, value in raw.items()}
        count = fields.get("count")
        digest = fields.get("digest")
        if not isinstance(count, int) or not isinstance(digest, str):
            msg = "a fingerprint needs an integer count and a string digest"
            raise TypeError(msg)
        return cls(count=count, digest=digest)

    def as_stored(self) -> dict[str, int | str]:
        """The committed JSON form."""
        return {"count": self.count, "digest": self.digest}


@dataclass(frozen=True)
class GateSource:
    """The registry class one CI gate scans."""

    gate: str
    env_var: str
    registry_class: str


#: Only the gates CI feeds from a secret. The diff/core gates run locally off the
#: DB, so there is no CI-visible copy of theirs to drift.
GATES: Final[tuple[GateSource, ...]] = (
    GateSource("tree", "TEATREE_TERM_REGISTRY", "leak"),
    GateSource("overlay", "TEATREE_TERM_REGISTRY", "overlay"),
)


class TermStore:
    """The operator's configured lists, read from the DB-home ``ConfigSetting`` rows."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path

    def _setting(self, key: str) -> object:
        """Read one row through teatree's Django-free cold reader.

        Imported lazily so ``check-ci`` — which touches no store — runs on the
        stdlib alone, keeping the CI job free of a dependency sync.
        """
        from teatree.config import cold_reader

        return cold_reader.read_setting(key, db_path=self.db_path)

    def _registry(self) -> dict[str, tuple[str, ...]] | None:
        """The consolidated registry as ``{class: terms}``, or ``None`` when unset."""
        raw = self._setting("banned_term_registry")
        if not isinstance(raw, dict):
            return None
        return {str(key): normalise_terms(value) for key, value in raw.items()}

    def registry_value(self) -> dict | None:
        """Return the classed value to copy to the CI secret."""
        value = self._setting("banned_term_registry")
        return value if isinstance(value, dict) else None

    def configured(self, source: GateSource) -> tuple[str, ...]:
        """The terms configured for *source* in the single registry."""
        registry = self._registry()
        return registry.get(source.registry_class, ()) if registry is not None else ()

    def fingerprints(self) -> dict[str, Fingerprint]:
        """Fingerprint every CI-fed gate's configured list."""
        return {source.gate: Fingerprint.of(self.configured(source)) for source in GATES}


@dataclass(frozen=True)
class FingerprintDocument:
    """The committable contract: one fingerprint per CI-fed gate, and nothing else."""

    version: int
    gates: dict[str, Fingerprint]

    @classmethod
    def from_store(cls, store: TermStore) -> "FingerprintDocument":
        """Build the document from what the operator has configured."""
        return cls(version=1, gates=store.fingerprints())

    @classmethod
    def read(cls, path: Path) -> "FingerprintDocument":
        """Load a committed document."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        gates = payload.get("gates") if isinstance(payload, dict) else None
        if not isinstance(gates, dict):
            msg = f"{path} carries no gates table"
            raise TypeError(msg)
        version = payload.get("version", 1)
        return cls(
            version=version if isinstance(version, int) else 1,
            gates={str(gate): Fingerprint.from_stored(raw) for gate, raw in gates.items()},
        )

    def write(self, path: Path) -> None:
        """Persist the document, sorted so a resync produces a minimal diff."""
        payload = {
            "version": self.version,
            "note": _DOCUMENT_NOTE,
            "gates": {gate: fingerprint.as_stored() for gate, fingerprint in self.gates.items()},
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class DriftDetector:
    """The four operations, each returning a process exit code."""

    def __init__(self, store: TermStore) -> None:
        self.store = store

    def generate(self, out: Path) -> int:
        """Write the fingerprint of the operator's configured lists to *out*."""
        document = FingerprintDocument.from_store(self.store)
        document.write(out)
        for gate, fingerprint in sorted(document.gates.items()):
            print(f"{gate}: {fingerprint.count} term(s)")
        print(f"wrote {out}")
        return OK

    def sync(self, repo: str, fingerprint_path: Path, *, apply: bool) -> int:
        """Push the classed registry to its one secret, then refresh the fingerprint.

        The list is streamed to ``gh secret set`` on stdin, so no term value is ever
        an argument, an environment variable, or a line of output.
        """
        import subprocess

        registry = self.store.registry_value()
        if not isinstance(registry, dict):
            print("banned_term_registry is unset or malformed.")
            return MISCONFIGURED
        if not apply:
            print("would set TEATREE_TERM_REGISTRY to the classed registry (dry run).")
            return OK
        result = subprocess.run(
            ["gh", "secret", "set", "TEATREE_TERM_REGISTRY", "--repo", repo],
            input=json.dumps(registry),
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"FAILED to set $TEATREE_TERM_REGISTRY (gh exit {result.returncode}).")
            return MISCONFIGURED
        if not apply:
            print(f"\nDry run — rerun with --apply to write the secrets and refresh {fingerprint_path}.")
            return OK
        self.generate(fingerprint_path)
        print("\nCommit the refreshed fingerprint so CI can detect the next divergence.")
        return OK


def check_ci(fingerprint_path: Path) -> int:
    """Compare each gate's CI-visible list against the committed fingerprint.

    A free function, not a :class:`DriftDetector` method: it reads only the
    environment and the committed file, so it must stay runnable where no store
    exists at all — which is exactly the CI runner.
    """
    expected = FingerprintDocument.read(fingerprint_path).gates
    status = OK
    for source in GATES:
        want = expected.get(source.gate)
        if want is None:
            print(f"{source.gate}: no committed fingerprint — regenerate {fingerprint_path}")
            status = max(status, MISCONFIGURED)
            continue
        try:
            visible = terms_from_env(source.env_var, source.registry_class)
        except (TypeError, ValueError) as exc:
            print(f"{source.gate}: MISCONFIGURED — {exc}")
            status = max(status, MISCONFIGURED)
            continue
        if visible is None:
            print(
                f"{source.gate}: MISCONFIGURED — ${source.env_var} is unset, so the gate "
                f"cannot scan the {want.count} configured term(s)."
            )
            status = max(status, MISCONFIGURED)
            continue
        actual = Fingerprint.of(visible)
        if actual.count != want.count:
            print(
                f"{source.gate}: DRIFT — ${source.env_var} holds {actual.count} term(s); "
                f"the committed fingerprint expects {want.count}."
            )
            status = max(status, DRIFT)
        elif actual.digest != want.digest:
            print(
                f"{source.gate}: DRIFT — ${source.env_var} holds {actual.count} term(s) as "
                f"expected but the digest differs, so the contents diverged."
            )
            status = max(status, DRIFT)
        else:
            print(f"{source.gate}: in sync ({actual.count} term(s)).")
    if status == DRIFT:
        print("\nResync the repository secrets: python scripts/term_source_drift.py sync --apply")
    return status


def build_parser() -> argparse.ArgumentParser:
    """The detector's command-line surface."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    generate_parser = sub.add_parser("generate", help="write the fingerprint of the configured lists")
    generate_parser.add_argument("--db", type=Path, default=None, help="ConfigSetting DB to read")
    generate_parser.add_argument("--out", type=Path, default=DEFAULT_FINGERPRINT)

    ci_parser = sub.add_parser("check-ci", help="compare the CI-visible lists to the committed fingerprint")
    ci_parser.add_argument("--fingerprint", type=Path, default=DEFAULT_FINGERPRINT)

    sync_parser = sub.add_parser("sync", help="push the configured lists to their secrets and refresh the fingerprint")
    sync_parser.add_argument("--db", type=Path, default=None, help="ConfigSetting DB to read")
    sync_parser.add_argument("--repo", default="souliane/teatree")
    sync_parser.add_argument("--fingerprint", type=Path, default=DEFAULT_FINGERPRINT)
    sync_parser.add_argument("--apply", action="store_true", help="actually write the secrets")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch a subcommand and return its exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "check-ci":
        return check_ci(args.fingerprint)
    detector = DriftDetector(TermStore(args.db))
    if args.command == "generate":
        return detector.generate(args.out)
    return detector.sync(args.repo, args.fingerprint, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
