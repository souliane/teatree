"""Cross-tier artifact parity ratchet — a Django-free reader and its Django writer must be pinned together.

The single most-repeated regression class in this repo: a concept is resolved by
BOTH a Django path and a Django-free "cold" path, the two read different sources,
they disagree silently, and every test that exists sits on one side of the divide.

Three shipped instances, all the same shape:

* **#3499** — ``teatree_settings.py`` cold reads resolved to compiled-in defaults
    for months; 14 kill-switches silently ignored their stored values, because the
    value alone cannot distinguish "never opted in" from "cannot read the store".
* **#3819** — ``run-hook.sh`` picked an interpreter with no Django, so
    ``bootstrap_teatree_django()`` returned False and 18 DB-backed call sites
    silently no-opped.
* **#3826** — ``availability_override.json`` held a week-old ``autonomous_away``
    while the ``ModeOverride`` table was empty. The hooks obeyed the FILE; the guard
    built for exactly this failure (``_check_mode_override_staleness``) reads the
    TABLE, so it could never observe it. Both mirror and reader are now deleted —
    the instance is cited for its SHAPE, which this lane exists to catch again.

The repo already invented the countermeasure — ``tests/test_speak_hook_config_parity.py``
pins the hook-side ``speak`` duplicate against the config-side source of truth,
and ``tests/config/test_cold_hook_settings.py`` does it for the cold kill-switch
registry. What was missing is TOTALITY: nothing forced a NEW cross-tier artifact
to acquire such a pin, so coverage accreted one concept at a time and the gaps
were invisible.

This lane is that totality ratchet. Discovery is mechanical and tree-wide, never
a hand-list: an artifact-shaped filename literal that appears BOTH under
``src/teatree`` (the Django tier) AND under a Django-free consumer root
(``hooks/``, ``scripts/lib/``, including ``*.sh``) IS a cross-tier artifact, by
construction. Each one must then be enrolled either as covered — naming a test
that provably exercises BOTH tiers — or as an explicitly-pegged gap carrying its
tracking issue.

Crucially the coverage criterion is NOT "some test mentions the filename". Every
live mirror has such a test, and #3826's mirror had four of them — it shipped
divergent anyway. Substring presence is the same measurement-by-proxy that let
"237/237 keys in the DOM" pass while no row was usable. A covering test must
IMPORT a ``teatree.*`` module and a ``hooks.*``/``scripts.*`` module in the same
file — it must be able to observe both sides of the seam it claims to pin.

The peg ledger is a ratchet in both directions: a new unpinned artifact fails
(:class:`TestCrossTierRosterIsComplete`), and a pegged artifact that has silently
GAINED a both-tier test must be promoted out of the ledger
(:class:`TestCrossTierPegLedgerRatchets`) — freed budget can never be respent on
the next gap.
"""

import ast
import re
from functools import cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The Django tier — the whole package, never a hardcoded subset.
_DJANGO_ROOT = _REPO_ROOT / "src" / "teatree"
#: The Django-free tiers: the hook leaves and the stdlib-only script helpers.
#: These run before (or entirely without) a Django bootstrap, which is precisely
#: why they carry their own readers and can drift.
_COLD_ROOTS = (_REPO_ROOT / "hooks", _REPO_ROOT / "scripts" / "lib")

_TESTS_ROOT = _REPO_ROOT / "tests"

#: A string literal shaped like an on-disk artifact. Deliberately narrow — a bare
#: word or a path fragment is not an artifact; a concrete ``<name>.<ext>`` is.
_ARTIFACT_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(json|txt|md|sqlite3|toml|yaml|yml|log|html)$")

#: Artifacts that are NOT a Django-state mirror, with the reason each is exempt.
#: Reviewable by construction: an entry here is a claim that no state flows from
#: the ORM into the file, so the two tiers have nothing to disagree ABOUT.
NOT_A_CROSS_TIER_MIRROR: dict[str, str] = {
    "CLAUDE.md": "repo/agent instruction prose — authored, never derived from ORM state",
    "SKILL.md": "skill frontmatter authored in-repo; schema-validated by the validate-skill-md prek hook",
    "pyproject.toml": "build metadata — authored, never written from the ORM",
    "settings.json": "the Claude Code harness's own config file, not a teatree-written mirror",
    "hooks.json": "the shipped plugin's hook manifest — authored in-repo, never written from the ORM",
    "db.sqlite3": (
        "the canonical store ITSELF, not a mirror of it. Its cross-tier reads are the "
        "COLD_HOOK_SETTINGS registry, already pinned key-by-key by "
        "tests/config/test_cold_hook_settings.py plus the t3 doctor cold-hook probe"
    ),
}

#: Cross-tier artifact -> a test that provably exercises BOTH tiers.
#: An entry here is a claim of real coverage, and
#: ``test_every_covered_artifact_has_a_both_tier_test`` verifies the claim.
PARITY_LANE_ROSTER: dict[str, str] = {
    "loop-registry.json": "tests/test_session_start_bootstrap_hook.py",
    "tick-meta.json": "tests/test_hook_router_cadence_hook.py",
    "consolidation-registry.json": "tests/test_consolidation_registry_parity.py",
    "skill-metadata.json": "tests/test_skill_metadata_cache_parity.py",
    "statusline.txt": "tests/test_statusline_shell_parity.py",
    "host-projection.json": "tests/test_statusline_shell_parity.py",
}

#: Cross-tier artifacts with NO both-tier pin yet — the ratchet ledger.
#: Every row carries its tracking issue. Rows may only be REMOVED (by adding a
#: real both-tier test and promoting the artifact into PARITY_LANE_ROSTER); a new
#: row is a new instance of the #3826 class and must be justified in review.
#: Empty is the goal state, not a bug: every discovered cross-tier artifact is
#: either covered above or exempted below.
UNPINNED_CROSS_TIER_MIRRORS: dict[str, str] = {}

_ISSUE_URL_SHAPE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/issues/\d+$")

#: Anti-vacuity floors — a discovery that silently stopped finding things must not pass green.
_MIN_DJANGO_LITERALS = 40
_MIN_COLD_LITERALS = 10
_MIN_CROSS_TIER = 8
#: Artifacts known to live in DISTINCT cold roots / file types, so a re-narrowed
#: scan (dropping ``*.sh``, or dropping ``scripts/lib``) is caught.
_CROSS_ROOT_ANCHORS: frozenset[str] = frozenset(
    {
        "consolidation-registry.json",  # hooks/scripts/*.py only
        "statusline.txt",  # hooks/scripts/*.sh only
        "skill-metadata.json",  # scripts/lib/*.py only
    }
)


@cache
def _literals_in_python(root: Path) -> dict[str, set[str]]:
    """Artifact-shaped string constants under *root*, keyed by literal -> owning modules.

    Migrations are FROZEN ORM state, never a live reader or writer, so they are
    excluded for the same reason ``test_user_settings_readers`` excludes them.

    Cached: the tree does not change within a run, and every assertion below needs
    the same walk. Uncached, this lane cost ~12s of the push gate's conformance
    budget for six repeats of identical work.
    """
    found: dict[str, set[str]] = {}
    for path in root.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _ARTIFACT_SHAPE.match(node.value):
                found.setdefault(node.value, set()).add(path.relative_to(_REPO_ROOT).as_posix())
    return found


@cache
def _literals_in_shell(root: Path) -> dict[str, set[str]]:
    """Artifact-shaped tokens in ``*.sh`` under *root*.

    A shell hook reading the store with a raw ``sqlite3`` call is a real cold
    reader that a Python-only walk cannot see — the same blind spot
    ``test_user_settings_readers`` closes with its own ``_shell_readers`` pass.
    """
    token = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.(?:json|txt|md|sqlite3|toml|yaml|yml|log|html)")
    found: dict[str, set[str]] = {}
    for path in root.rglob("*.sh"):
        for match in token.findall(path.read_text(encoding="utf-8", errors="ignore")):
            found.setdefault(match, set()).add(path.relative_to(_REPO_ROOT).as_posix())
    return found


def django_tier_literals() -> dict[str, set[str]]:
    return _literals_in_python(_DJANGO_ROOT)


def cold_tier_literals() -> dict[str, set[str]]:
    merged: dict[str, set[str]] = {}
    for root in _COLD_ROOTS:
        if not root.is_dir():
            continue
        for source in (_literals_in_python(root), _literals_in_shell(root)):
            for name, owners in source.items():
                merged.setdefault(name, set()).update(owners)
    return merged


def cross_tier_artifacts() -> set[str]:
    """Artifacts named by BOTH tiers — the unit of coverage, discovered not declared."""
    return set(django_tier_literals()) & set(cold_tier_literals())


@cache
def _imported_modules(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
    except SyntaxError:
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _reaches_both_tiers(path: Path) -> bool:
    """*path* imports a Django-tier module AND a Django-free-tier module.

    The discriminator that makes this lane more than a substring check: a test
    that cannot even import both sides cannot have compared them.
    """
    modules = _imported_modules(path)
    return any(m.startswith("teatree.") for m in modules) and any(m.startswith(("hooks.", "scripts.")) for m in modules)


@cache
def _both_tier_test_files() -> tuple[Path, ...]:
    """Every test file that imports both tiers — the only candidates for a real pin.

    Computed once: the both-tier set is small, so the per-artifact search below is a
    substring check over a handful of files rather than a walk of the whole suite.
    """
    return tuple(path for path in sorted(_TESTS_ROOT.rglob("test_*.py")) if _reaches_both_tiers(path))


def both_tier_tests_for(artifact: str) -> list[str]:
    """Test files that name *artifact* AND import both tiers."""
    return [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _both_tier_test_files()
        if artifact in path.read_text(encoding="utf-8", errors="ignore")
    ]


class TestCrossTierRosterIsComplete:
    """Every discovered cross-tier artifact is accounted for — no silent-coverage gap."""

    def test_every_cross_tier_artifact_is_enrolled(self) -> None:
        enrolled = set(PARITY_LANE_ROSTER) | set(UNPINNED_CROSS_TIER_MIRRORS) | set(NOT_A_CROSS_TIER_MIRROR)
        unenrolled = sorted(cross_tier_artifacts() - enrolled)
        assert not unenrolled, (
            "artifact(s) read by BOTH the Django tier and a Django-free tier with no parity enrolment — "
            "this is the #3499 / #3819 / #3826 class. Add a test that imports both tiers and enrol it in "
            "PARITY_LANE_ROSTER; or, if the two tiers genuinely cannot disagree, add a rationale row to "
            "NOT_A_CROSS_TIER_MIRROR: " + str(unenrolled)
        )

    def test_no_roster_entry_is_a_phantom_artifact(self) -> None:
        live = cross_tier_artifacts()
        phantom = sorted(name for name in PARITY_LANE_ROSTER if name not in live)
        assert not phantom, f"PARITY_LANE_ROSTER rows naming no live cross-tier artifact (renamed/removed): {phantom}"

    def test_no_peg_ledger_entry_is_a_phantom_artifact(self) -> None:
        live = cross_tier_artifacts()
        phantom = sorted(name for name in UNPINNED_CROSS_TIER_MIRRORS if name not in live)
        assert not phantom, (
            "UNPINNED_CROSS_TIER_MIRRORS rows naming no live cross-tier artifact — the artifact was "
            f"renamed or removed, so drop the row: {phantom}"
        )

    def test_every_covered_artifact_has_a_both_tier_test(self) -> None:
        for artifact, test_rel in PARITY_LANE_ROSTER.items():
            path = _REPO_ROOT / test_rel
            assert path.is_file(), f"{artifact}: covering lane file {test_rel} is missing"
            text = path.read_text(encoding="utf-8")
            assert artifact in text, f"{artifact}: covering lane {test_rel} no longer references it"
            assert _reaches_both_tiers(path), (
                f"{artifact}: covering lane {test_rel} imports only ONE tier, so it cannot have compared "
                "them — a claim of coverage that mentions the filename without observing the seam"
            )


class TestCrossTierPegLedgerRatchets:
    """The ledger may only shrink — freed budget can never be respent silently."""

    def test_every_pegged_artifact_names_a_tracking_issue(self) -> None:
        malformed = sorted(
            f"{artifact} -> {issue!r}"
            for artifact, issue in UNPINNED_CROSS_TIER_MIRRORS.items()
            if not _ISSUE_URL_SHAPE.match(issue)
        )
        assert not malformed, f"peg rows must carry a tracking issue URL: {malformed}"

    def test_no_pegged_artifact_has_silently_gained_coverage(self) -> None:
        # Under-peg, the mandatory half of a ratchet: once a real both-tier test
        # lands, the row MUST move to PARITY_LANE_ROSTER. Leaving it pegged would
        # let the next gap hide behind a stale allowance.
        promotable = {
            artifact: covering
            for artifact in UNPINNED_CROSS_TIER_MIRRORS
            if (covering := both_tier_tests_for(artifact))
        }
        assert not promotable, (
            "pegged artifact(s) now HAVE a both-tier test — promote each into PARITY_LANE_ROSTER and "
            f"delete its UNPINNED_CROSS_TIER_MIRRORS row: {promotable}"
        )

    def test_the_two_ledgers_are_disjoint(self) -> None:
        both = sorted(set(PARITY_LANE_ROSTER) & set(UNPINNED_CROSS_TIER_MIRRORS))
        assert not both, f"artifact(s) claimed as both covered and unpinned: {both}"

    def test_no_exempt_artifact_is_also_enrolled(self) -> None:
        clashing = sorted(set(NOT_A_CROSS_TIER_MIRROR) & (set(PARITY_LANE_ROSTER) | set(UNPINNED_CROSS_TIER_MIRRORS)))
        assert not clashing, f"artifact(s) declared exempt AND enrolled: {clashing}"


class TestCrossTierScanIsTreeWide:
    """Self-completeness — the scan cannot silently re-narrow to a subset."""

    def test_django_scan_root_is_the_whole_package(self) -> None:
        assert _DJANGO_ROOT.name == "teatree"
        assert (_DJANGO_ROOT / "__init__.py").is_file()

    def test_cold_roots_all_exist(self) -> None:
        missing = [str(root) for root in _COLD_ROOTS if not root.is_dir()]
        assert not missing, f"cold-tier scan root(s) missing — scope narrowed?: {missing}"

    def test_scan_reaches_every_cold_root_and_file_type(self) -> None:
        # The anchors are reachable ONLY via distinct roots/extensions: dropping the
        # shell pass loses statusline.txt, dropping scripts/lib loses skill-metadata.json.
        missing = sorted(_CROSS_ROOT_ANCHORS - cross_tier_artifacts())
        assert not missing, f"scan missed cross-root anchor(s) — a tier or file type was dropped: {missing}"


class TestCrossTierCardinalityFloors:
    """Anti-vacuity — a discovery that finds nothing must not pass green."""

    def test_discovery_floors(self) -> None:
        django = django_tier_literals()
        cold = cold_tier_literals()
        assert len(django) >= _MIN_DJANGO_LITERALS, sorted(django)
        assert len(cold) >= _MIN_COLD_LITERALS, sorted(cold)
        assert len(cross_tier_artifacts()) >= _MIN_CROSS_TIER, sorted(cross_tier_artifacts())

    def test_enrolment_ledgers_are_non_empty(self) -> None:
        assert PARITY_LANE_ROSTER
        assert NOT_A_CROSS_TIER_MIRROR


class TestCrossTierFiresRed:
    """Anti-vacuity — the ratchet must actually catch the shapes it exists to catch."""

    def test_a_synthetic_unenrolled_artifact_is_reported(self) -> None:
        synthetic = "synthetic-mirror.json"
        assert _ARTIFACT_SHAPE.match(synthetic)
        enrolled = set(PARITY_LANE_ROSTER) | set(UNPINNED_CROSS_TIER_MIRRORS) | set(NOT_A_CROSS_TIER_MIRROR)
        assert synthetic not in enrolled

    def test_a_shipped_artifact_is_discovered_and_enrolled(self) -> None:
        # The composed anchor, on a REAL artifact rather than the synthetic one above:
        # loop-registry.json is written by src/teatree/core/session_identity.py and read
        # by hooks/scripts/hook_router.py, so a walk that silently stopped discovering
        # things loses it and this goes red. The literal is hardcoded, NOT read from a
        # ledger, so emptying both ledgers cannot make the assertion vacuous.
        # Deliberately asserts DISCOVERY + enrolment, never "still uncovered" — pinning
        # a gap open would make its eventual fix red a second, unrelated test.
        assert "loop-registry.json" in cross_tier_artifacts()
        assert "loop-registry.json" in set(PARITY_LANE_ROSTER) | set(UNPINNED_CROSS_TIER_MIRRORS)

    def test_mentioning_the_filename_is_not_coverage(self, tmp_path: Path) -> None:
        # The measurement-by-proxy guard: a test that names the artifact but imports
        # one tier must NOT read as covering. Four such files named the #3826 mirror,
        # which is exactly why it shipped divergent.
        one_tier = tmp_path / "test_one_tier.py"
        one_tier.write_text(
            "from teatree.core import session_identity\n\ndef test_x():\n    assert 'loop-registry.json'\n",
            encoding="utf-8",
        )
        assert not _reaches_both_tiers(one_tier)

        two_tier = tmp_path / "test_two_tier.py"
        two_tier.write_text(
            "from teatree.core import session_identity\n"
            "from hooks.scripts import hook_router\n\n"
            "def test_x():\n    assert session_identity and hook_router\n",
            encoding="utf-8",
        )
        assert _reaches_both_tiers(two_tier)

    def test_the_artifact_shape_is_selective(self) -> None:
        # The heuristic must not sweep in unrelated literals (a false ratchet trip).
        assert not _ARTIFACT_SHAPE.match("some plain sentence")
        assert not _ARTIFACT_SHAPE.match("teatree.core.models")
        assert not _ARTIFACT_SHAPE.match("/abs/path/to/dir")
        assert _ARTIFACT_SHAPE.match("loop-registry.json")
        assert _ARTIFACT_SHAPE.match("max_migration.txt")
