#!/usr/bin/env python3
"""Term-source drift detector — keeps every leak gate's list identical to the operator's.

Teatree's leak gates read their term lists from three places, and nothing used to
notice when they diverged:

* The ``tree`` gate (CI job ``banned-terms-tree``) reads ``$TEATREE_BANNED_BRANDS``,
    which the operator configures as the ``banned_brands`` row or the registry's
    ``leak`` class.
* The ``overlay`` gate (CI job ``overlay-leak-tree``) reads
    ``$TEATREE_OVERLAY_LEAK_TERMS``, configured as the ``overlay_leak_terms`` row
    or the registry's ``overlay`` class.

Two independent divergences are possible, and both have happened:

**CI drift.** The secret is a point-in-time copy. Nothing re-reads the DB, so a
term added locally never reaches CI and the full-tree backstop keeps running an
older, shorter list. ``check-ci`` closes this by comparing the CI-visible list
against a committed, term-free fingerprint.

**Registry shadowing.** Every gate resolves registry-FIRST, so a
``banned_term_registry`` class that lags its legacy row silently SHRINKS the
gate — a class the registry omits entirely reduces that gate to zero terms while
the legacy row still looks populated. ``check-shadow`` closes this by requiring
each registry class to cover its legacy row.

**Nothing here ever emits a term value.** Every report is counts plus a
domain-separated SHA-256 over the whole sorted list. A whole-list digest is not a
per-term oracle — it confirms only an exact guess of the entire list — which is
what makes the fingerprint safe to commit to a public repo.

Exit codes: ``0`` in sync, ``1`` drift detected, ``2`` misconfigured.
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


def terms_from_env(env_var: str) -> tuple[str, ...] | None:
    """The comma-separated list in *env_var*, or ``None`` when it is unset/empty.

    ``None`` is the "CI cannot see this list" signal — a fork PR, which cannot
    read repository secrets, rather than an empty list the operator chose.
    """
    raw = os.environ.get(env_var, "")
    if not raw.strip():
        return None
    return normalise_terms(raw.split(","))


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
    """Where one gate's term list lives in each of the three stores."""

    gate: str
    env_var: str
    registry_class: str
    legacy_key: str


#: Only the gates CI feeds from a secret. The diff/core gates run locally off the
#: DB, so there is no CI-visible copy of theirs to drift.
GATES: Final[tuple[GateSource, ...]] = (
    GateSource("tree", "TEATREE_BANNED_BRANDS", "leak", "banned_brands"),
    GateSource("overlay", "TEATREE_OVERLAY_LEAK_TERMS", "overlay", "overlay_leak_terms"),
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

    def configured(self, source: GateSource) -> tuple[str, ...]:
        """The WIDEST list configured for *source* — the registry class plus the legacy row.

        Widest, not registry-first: a narrower registry class is a defect
        (:meth:`shadowed_count` reports it), never a reason to shrink what CI enforces.
        """
        legacy = normalise_terms(self._setting(source.legacy_key))
        registry = self._registry()
        from_registry = registry.get(source.registry_class, ()) if registry is not None else ()
        return tuple(sorted(set(legacy) | set(from_registry)))

    def shadowed_count(self, source: GateSource) -> int:
        """How many of *source*'s legacy-row terms its registry class fails to carry.

        ``0`` when the registry is unset — pre-cutover every gate reads its legacy
        row, so nothing is shadowed.
        """
        registry = self._registry()
        if registry is None:
            return 0
        legacy = set(normalise_terms(self._setting(source.legacy_key)))
        return len(legacy - set(registry.get(source.registry_class, ())))

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

    def check_shadow(self) -> int:
        """Fail when a registry class carries fewer terms than the legacy row it replaced."""
        status = OK
        for source in GATES:
            missing = self.store.shadowed_count(source)
            if missing:
                print(
                    f"{source.gate}: SHADOWED — the registry {source.registry_class!r} class omits "
                    f"{missing} term(s) the {source.legacy_key!r} row carries, so registry-first "
                    f"resolution shrinks this gate."
                )
                status = DRIFT
            else:
                print(f"{source.gate}: registry covers {source.legacy_key!r}.")
        if status == DRIFT:
            print("\nRebuild the registry from the legacy rows so no class lags its row.")
        return status

    def sync(self, repo: str, fingerprint_path: Path, *, apply: bool) -> int:
        """Push each gate's configured list to its secret, then refresh the fingerprint.

        The list is streamed to ``gh secret set`` on stdin, so no term value is ever
        an argument, an environment variable, or a line of output.
        """
        import subprocess

        if self.check_shadow() != OK:
            print("\nSyncing the WIDEST configured list regardless — a lagging registry never shrinks CI.\n")
        for source in GATES:
            terms = self.store.configured(source)
            if not terms:
                print(f"{source.gate}: nothing configured — leaving ${source.env_var} untouched.")
                continue
            if not apply:
                print(f"{source.gate}: would set {source.env_var} to {len(terms)} term(s) (dry run).")
                continue
            result = subprocess.run(
                ["gh", "secret", "set", source.env_var, "--repo", repo],
                input=",".join(terms),
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                print(f"{source.gate}: FAILED to set ${source.env_var} (gh exit {result.returncode}).")
                return MISCONFIGURED
            print(f"{source.gate}: set ${source.env_var} to {len(terms)} term(s).")
        if not apply:
            print(f"\nDry run — rerun with --apply to write the secrets and refresh {fingerprint_path}.")
            return OK
        self.generate(fingerprint_path)
        print("\nCommit the refreshed fingerprint so CI can detect the next divergence.")
        return OK


def check_ci(fingerprint_path: Path, *, allow_unset: bool) -> int:
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
        visible = terms_from_env(source.env_var)
        if visible is None:
            if allow_unset:
                print(f"{source.gate}: SKIPPED — ${source.env_var} is unreadable here (fork PR).")
                continue
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
    ci_parser.add_argument(
        "--allow-unset",
        action="store_true",
        help="treat an unreadable secret as a clean skip (a fork PR) instead of misconfigured",
    )

    shadow_parser = sub.add_parser("check-shadow", help="require each registry class to cover its legacy row")
    shadow_parser.add_argument("--db", type=Path, default=None, help="ConfigSetting DB to read")

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
        return check_ci(args.fingerprint, allow_unset=args.allow_unset)
    detector = DriftDetector(TermStore(args.db))
    if args.command == "generate":
        return detector.generate(args.out)
    if args.command == "check-shadow":
        return detector.check_shadow()
    return detector.sync(args.repo, args.fingerprint, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
