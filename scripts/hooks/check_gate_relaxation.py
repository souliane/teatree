"""Pre-commit hook: anti-relaxation + tach-soundness gate (BLUEPRINT §17.6, #850).

Refuses a commit whose STAGED diff relaxes a lint/coverage constraint or a tach
module boundary without the sanctioned relax marker — a new unjustified
``# noqa``, a new ``per-file-ignores`` / coverage ``omit`` entry, a lowered
``fail_under``, a committed ``--no-verify``, a new empty ``interfaces = []``, or
a new ``ignore_type_checking_imports`` with no justifying comment. Findings key off
the diff's ADDED lines, so the boilerplate baseline present before the gate was
deployed is exempt for free; ``# noqa`` findings are additionally diff-aware — a
code already suppressed on a line at base is not flagged when it reappears
because sibling codes were stripped, only a genuinely-new suppression is.

Enforcement (§17.6.5 WARN-not-hardfail): a BLOCK finding refuses the commit; a
WARN finding (possible test vacuity — a fuzzy heuristic) prints advisory-only
and never fails. Never-lockout: the ``ALLOW_GATE_RELAX=<reason>`` env marker
(a non-empty reason, mirroring ``ALLOW_BANNED_TERM=1``) records a sanctioned
relaxation and lets the commit through, and the ``gate_relaxation_gate_enabled``
kill-switch disables the gate entirely — resolved DB-first through
``get_effective_settings`` (the canonical resolver every sibling gate uses), so a
DB ``config_setting set gate_relaxation_gate_enabled false`` actuates the hook.
The diff scan is the fast Django-free
pre-check: only a finding worth acting on pays the Django bootstrap for the
kill-switch read. Any internal error FAILS OPEN — a gate bug must never wedge a
commit.
"""

import os
import subprocess
import sys

# Importable because prek runs this as ``uv run python`` with teatree installed;
# the scan engine is pure and lives in the teatree package (single source of
# truth shared with ``t3 tool gate-relaxation``).
from teatree.quality.gate_relaxation import BLOCK, WARN, scan_relaxation

_ALLOW_ENV = "ALLOW_GATE_RELAX"


def _gate_enabled() -> bool:
    """Resolve ``gate_relaxation_gate_enabled`` via the canonical DB-first resolver.

    Reads the kill-switch through ``get_effective_settings`` — the same DB-home
    resolver every sibling gate consults (see ``check_snapshot_baseline``) — so a
    DB ``config_setting set gate_relaxation_gate_enabled false`` actuates the hook.
    Requires Django, so the caller reads
    it only once the diff scan has surfaced a finding worth acting on; the DB read
    fails safe to the dataclass default (ENABLED) when Django/the DB is
    unavailable, so only an explicit ``false`` disables the gate.
    """
    from teatree.config import get_effective_settings
    from teatree.utils.django_bootstrap import ensure_django

    ensure_django()
    return bool(get_effective_settings().gate_relaxation_gate_enabled)


def _staged_diff() -> str:
    result = subprocess.run(
        ["git", "diff", "--cached", "--src-prefix=a/", "--dst-prefix=b/"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def _scan_and_decide(diff: str) -> int:
    findings = scan_relaxation(diff)
    if not findings:
        return 0
    if not _gate_enabled():
        return 0
    for finding in (f for f in findings if f.severity == WARN):
        print(f"WARN: {finding.path}: {finding.message}", file=sys.stderr)
    blocking = [f for f in findings if f.severity == BLOCK]
    if not blocking:
        return 0
    marker = os.environ.get(_ALLOW_ENV, "").strip()
    if marker:
        print(f"NOTE: gate-relaxation allowed via {_ALLOW_ENV}={marker!r} — {len(blocking)} relaxation(s) sanctioned.")
        return 0
    print("BLOCKED: anti-relaxation gate (§17.6, #850) — this commit relaxes a gate/tach constraint:")
    for finding in blocking:
        print(f"  - {finding.path}: {finding.message}")
        if finding.line:
            print(f"      {finding.line}")
    print(
        f"\nFix the underlying issue (refactor, restore the floor, remove the suppression), or if the\n"
        f"relaxation is genuinely justified and human-approved, record it with "
        f"{_ALLOW_ENV}='<reason>' git commit ...\n"
    )
    return 1


def main() -> int:
    diff = _staged_diff()
    if not diff.strip():
        return 0
    try:
        return _scan_and_decide(diff)
    except Exception as exc:  # noqa: BLE001 — fail-open: a scan bug must never wedge commits repo-wide.
        print(f"WARN: anti-relaxation gate errored — failing open: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
