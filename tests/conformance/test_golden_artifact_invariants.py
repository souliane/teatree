"""Golden-artifact invariants — a byte comparison is not an assertion about correctness.

``tests/teatree_dash/test_snapshot.py`` re-renders ``/dash/board`` and asserts the
result byte-matches the committed ``src/teatree/dash/snapshots/board.html``. That
comparison is total and it is also entirely self-referential: its expected value
comes from the very renderer under test. When the renderer started emitting a
Django template comment as literal page text — ``{# … #}`` is single-line only,
so a comment wrapped across three lines renders — the fix for the drift test was
to regenerate the golden, and the suite then asserted the bug was the correct
output. It stayed asserted-correct until a human looked at the page (#3823).

The lesson generalises past that one file: **a golden compared only against its
own renderer has no invariant the renderer can violate.** It needs at least one
assertion whose truth does not depend on the renderer being right.

This lane supplies that floor for every golden in the tree, and pins the second
half of the same story — WHO maintains the golden:

* ``docs/generated/**`` is machine-maintained. CI's ``docs-drift`` job re-runs
    every generator and then ``git diff --exit-code docs/generated``, so a stale or
    hand-edited file fails. Fifteen artifacts live there; none has ever leaked.
* ``src/teatree/dash/snapshots/board.html`` is maintained BY HAND — its own test
    docstring tells a human to run a ``python -c`` one-liner and eyeball the diff.
    It is the single golden outside the drift gate, and it is the single golden
    carrying a renderer leak. That correlation is the finding, not a coincidence.

Both ledgers ratchet in both directions: a new golden that violates an invariant
or sits outside the drift gate fails, and a pegged golden that has since been
cleaned must be un-pegged (:class:`TestGoldenPegLedgersRatchet`), so a freed
allowance can never be silently respent on the next one.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Roots holding committed golden artifacts — files whose content is a recorded
#: render rather than authored source.
_GOLDEN_ROOTS = (_REPO_ROOT / "docs" / "generated",)
#: Snapshot directories anywhere under the package (the ``snapshots/`` convention).
_PACKAGE_ROOT = _REPO_ROOT / "src"

_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
#: The CI step shape that makes a root machine-maintained: regenerate, then refuse
#: any diff. Parsed from the workflow so deleting the step is itself caught.
_DRIFT_GATE_STEP = re.compile(r"git diff --exit-code (\S+)")

#: Unrendered Django template syntax surviving into rendered HTML. Every one of
#: these means the template engine emitted markup it was supposed to consume.
_TEMPLATE_LEAK = re.compile(r"\{#|#\}|\{%|%\}|\{\{|\}\}")
#: Python internals that must never reach a rendered artifact — a repr in place of
#: a value, or a traceback in place of a page.
_INTERNALS_LEAK = re.compile(r"<[A-Za-z_][\w.]* object at 0x[0-9a-fA-F]+|Traceback \(most recent call last\)")

#: A golden below this many bytes is very likely a render that returned nothing and
#: was then committed as the expected output.
_MIN_GOLDEN_BYTES = 100

#: Goldens with a KNOWN renderer leak, each pegged to its tracking issue. Rows may
#: only be removed. A NEW row is a new instance of the #3823 class.
GOLDENS_WITH_PEGGED_LEAKS: dict[str, str] = {
    # EMPTY, and the ratchet below keeps it that way. The #3823 row (board.html's
    # three lines of leaked `{# … #}`) was pegged after `dec5aa42` had already
    # closed the comment, so the peg shielded an artifact that no longer leaked —
    # which `test_no_pegged_leak_has_silently_been_fixed` refuses by design.
}

#: Goldens NOT covered by a regenerate-and-diff CI gate, pegged to their issue.
#: Hand-maintenance is how a renderer bug becomes an expected value.
GOLDENS_OUTSIDE_THE_DRIFT_GATE: dict[str, str] = {
    "src/teatree/dash/snapshots/board.html": "https://github.com/souliane/teatree/issues/3832",
}

_ISSUE_URL_SHAPE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/issues/\d+$")

#: Anti-vacuity floors.
_MIN_GOLDENS = 12
_MIN_HTML_GOLDENS = 2
#: Goldens reachable only via distinct discovery arms — a re-narrowed walk drops one.
_DISCOVERY_ANCHORS: frozenset[str] = frozenset(
    {
        "docs/generated/cli-reference.md",  # _GOLDEN_ROOTS arm
        "docs/generated/dashboard/admin-index.html",  # nested under _GOLDEN_ROOTS
        "src/teatree/dash/snapshots/board.html",  # package snapshots/ arm
    }
)


def golden_paths() -> list[Path]:
    """Every committed golden artifact, discovered by convention rather than listed."""
    found: set[Path] = set()
    for root in _GOLDEN_ROOTS:
        found.update(path for path in root.rglob("*") if path.is_file())
    for snapshot_dir in _PACKAGE_ROOT.rglob("snapshots"):
        if snapshot_dir.is_dir():
            found.update(path for path in snapshot_dir.rglob("*") if path.is_file())
    return sorted(found)


def _rel(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


def drift_gated_roots() -> set[str]:
    """Repo roots CI regenerates and then refuses to see a diff in.

    Read out of the workflow rather than hardcoded: if the ``git diff --exit-code``
    step is ever dropped, this set shrinks and the totality assertion below fails
    instead of silently passing on an ungated tree.
    """
    return set(_DRIFT_GATE_STEP.findall(_CI_WORKFLOW.read_text(encoding="utf-8")))


def _is_drift_gated(rel_path: str) -> bool:
    return any(rel_path == root or rel_path.startswith(f"{root.rstrip('/')}/") for root in drift_gated_roots())


class TestGoldenContentInvariants:
    """Renderer-independent floors — assertions the renderer cannot make true by being wrong."""

    def test_no_golden_is_empty(self) -> None:
        tiny = sorted(
            f"{_rel(path)} ({path.stat().st_size}B)"
            for path in golden_paths()
            if path.stat().st_size < _MIN_GOLDEN_BYTES
        )
        assert not tiny, (
            "golden artifact(s) below the size floor — a render that produced (almost) nothing and was "
            f"committed as the expected output: {tiny}"
        )

    def test_no_html_golden_leaks_unrendered_template_syntax(self) -> None:
        offenders = {}
        for path in golden_paths():
            if path.suffix != ".html" or _rel(path) in GOLDENS_WITH_PEGGED_LEAKS:
                continue
            leaks = _TEMPLATE_LEAK.findall(path.read_text(encoding="utf-8", errors="ignore"))
            if leaks:
                offenders[_rel(path)] = sorted(set(leaks))
        assert not offenders, (
            "rendered HTML golden(s) containing unrendered template syntax — the template engine emitted "
            "markup it should have consumed, and the byte comparison recorded it as correct. Note Django's "
            "`{# #}` comment is SINGLE-LINE only; use `{% comment %}` to span lines (#3823): " + str(offenders)
        )

    def test_no_golden_leaks_python_internals(self) -> None:
        offenders = {}
        for path in golden_paths():
            leaks = _INTERNALS_LEAK.findall(path.read_text(encoding="utf-8", errors="ignore"))
            if leaks:
                offenders[_rel(path)] = sorted(set(leaks))[:3]
        assert not offenders, (
            "golden artifact(s) containing a Python object repr or a traceback — an internal reached the "
            f"rendered output and the byte comparison froze it as expected: {offenders}"
        )


class TestGoldenMaintenanceIsMechanical:
    """Every golden is regenerated by a machine and diff-gated — never hand-edited."""

    def test_every_golden_is_under_a_drift_gated_root(self) -> None:
        ungated = sorted(
            _rel(path)
            for path in golden_paths()
            if not _is_drift_gated(_rel(path)) and _rel(path) not in GOLDENS_OUTSIDE_THE_DRIFT_GATE
        )
        assert not ungated, (
            "golden artifact(s) outside every regenerate-and-diff CI gate. A hand-maintained golden is how "
            "a renderer bug becomes an expected value (#3823): give it a generator and a "
            "`git diff --exit-code <root>` step in the docs-drift job: " + str(ungated)
        )

    def test_the_drift_gate_still_exists_in_ci(self) -> None:
        # Self-completeness: the assertion above is only meaningful while CI really
        # runs the step it reads. An empty set would make it vacuously strict, and a
        # DELETED step would make the whole lane meaningless.
        roots = drift_gated_roots()
        assert "docs/generated" in roots, (
            f"CI no longer runs `git diff --exit-code docs/generated` — the generated docs are unguarded: {roots}"
        )


class TestGoldenPegLedgersRatchet:
    """Both ledgers may only shrink — a cleaned golden must be un-pegged."""

    def test_every_peg_names_a_tracking_issue(self) -> None:
        malformed = sorted(
            f"{name} -> {issue!r}"
            for ledger in (GOLDENS_WITH_PEGGED_LEAKS, GOLDENS_OUTSIDE_THE_DRIFT_GATE)
            for name, issue in ledger.items()
            if not _ISSUE_URL_SHAPE.match(issue)
        )
        assert not malformed, f"peg rows must carry a tracking issue URL: {malformed}"

    def test_every_pegged_golden_still_exists(self) -> None:
        live = {_rel(path) for path in golden_paths()}
        phantom = sorted(
            name
            for ledger in (GOLDENS_WITH_PEGGED_LEAKS, GOLDENS_OUTSIDE_THE_DRIFT_GATE)
            for name in ledger
            if name not in live
        )
        assert not phantom, f"peg rows naming no live golden (renamed/removed) — drop the row: {phantom}"

    def test_no_pegged_leak_has_silently_been_fixed(self) -> None:
        # Under-peg, the mandatory half: once the renderer stops leaking, the row must
        # go, or the allowance shields the next leak in the same file.
        clean = sorted(
            name
            for name in GOLDENS_WITH_PEGGED_LEAKS
            if not _TEMPLATE_LEAK.search((_REPO_ROOT / name).read_text(encoding="utf-8", errors="ignore"))
        )
        assert not clean, (
            "pegged golden(s) no longer leak template syntax — delete the row from "
            f"GOLDENS_WITH_PEGGED_LEAKS so the file is guarded again: {clean}"
        )

    def test_no_ungated_peg_has_silently_become_gated(self) -> None:
        gated = sorted(name for name in GOLDENS_OUTSIDE_THE_DRIFT_GATE if _is_drift_gated(name))
        assert not gated, (
            "pegged golden(s) are now under a drift-gated root — delete the row from "
            f"GOLDENS_OUTSIDE_THE_DRIFT_GATE: {gated}"
        )


class TestGoldenDiscoveryFloors:
    """Anti-vacuity — a discovery that finds nothing must not pass green."""

    def test_discovery_floors(self) -> None:
        goldens = golden_paths()
        assert len(goldens) >= _MIN_GOLDENS, [_rel(p) for p in goldens]
        html = [p for p in goldens if p.suffix == ".html"]
        assert len(html) >= _MIN_HTML_GOLDENS, [_rel(p) for p in html]

    def test_discovery_reaches_every_arm(self) -> None:
        # The anchors are reachable only via distinct arms: dropping the package
        # `snapshots/` walk loses board.html, which is the one the lane exists for.
        missing = sorted(_DISCOVERY_ANCHORS - {_rel(path) for path in golden_paths()})
        assert not missing, f"golden discovery missed anchor(s) — an arm was dropped: {missing}"


class TestGoldenInvariantsFireRed:
    """Anti-vacuity — the detectors must actually catch the shapes they exist to catch."""

    def test_the_historical_3823_leak_is_detected(self) -> None:
        # The concrete render that shipped, quoted verbatim from `dec5aa42^`'s
        # board.html: the detector is proven against the REAL leaked bytes, not only
        # a synthetic one. Quoting them keeps that proof after the artifact is fixed
        # — the anchor must not require the bug to still be present, which is what
        # made it fire red once `dec5aa42` closed the comment.
        shipped_leak = (
            "      {# Always-visible loopback terminal: POSTs to the same debug_session endpoint\n"
            "      the drawer uses (a fresh generic session, no ticket id); body hx-headers\n"
            "      carries the CSRF token. #}\n"
        )
        assert _TEMPLATE_LEAK.search(shipped_leak)

    def test_the_historical_3823_artifact_is_clean_and_guarded(self) -> None:
        # The other half: board.html is fixed AND un-pegged, so the detector really
        # runs over it. A peg on a clean file silently shields the next leak.
        board = _REPO_ROOT / "src" / "teatree" / "dash" / "snapshots" / "board.html"
        assert not _TEMPLATE_LEAK.search(board.read_text(encoding="utf-8"))
        assert _rel(board) not in GOLDENS_WITH_PEGGED_LEAKS

    def test_detectors_fire_on_synthetic_offenders(self, tmp_path: Path) -> None:
        assert _TEMPLATE_LEAK.search("<div>{# stray comment #}</div>")
        assert _TEMPLATE_LEAK.search("<div>{% if x %}</div>")
        assert _INTERNALS_LEAK.search("<Ticket object at 0x7f0011aa>")
        assert _INTERNALS_LEAK.search("Traceback (most recent call last):")

    def test_detectors_are_selective(self) -> None:
        # A false trip would make the lane noise, and noise gets bypassed.
        assert not _TEMPLATE_LEAK.search('<div class="dash-actions"><button>Terminal</button></div>')
        assert not _INTERNALS_LEAK.search("the object at the top of the page")
        assert not _INTERNALS_LEAK.search("<span>0x1F</span>")

    def test_an_ungated_golden_root_is_reported(self) -> None:
        assert not _is_drift_gated("src/teatree/dash/snapshots/board.html")
        assert _is_drift_gated("docs/generated/cli-reference.md")
