"""Loader/schema invariants for the anti-pattern catalog.

These are the catalog↔eval / catalog↔linter reachability ledger: every named
``linter`` must resolve to a real ``scripts/hooks/*.py`` (or a known external
tool), and every non-null ``eval_invariant`` must resolve to a real invariant id
in ``transcript_conformance`` — so a catalog entry can never cite enforcement
that does not exist.
"""

import re
from pathlib import Path

import pytest

from teatree.eval.transcript_conformance import INVARIANT_REGISTRY
from teatree.quality.catalog import AntiPatternEntry, CatalogError, catalog_path, load_catalog

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOKS_DIR = _REPO_ROOT / "scripts" / "hooks"

# External tools (not scripts/hooks/*.py) that legitimately mechanize an entry.
_EXTERNAL_LINTERS = frozenset({"tach", "gate-liveness"})

_KNOWN_INVARIANT_IDS = frozenset(inv.id for inv in INVARIANT_REGISTRY)


@pytest.fixture(scope="module")
def catalog() -> tuple[AntiPatternEntry, ...]:
    return load_catalog()


class TestSchemaInvariants:
    def test_catalog_is_non_empty(self, catalog: tuple[AntiPatternEntry, ...]) -> None:
        assert catalog

    def test_ids_are_unique(self, catalog: tuple[AntiPatternEntry, ...]) -> None:
        ids = [e.id for e in catalog]
        assert len(ids) == len(set(ids))

    def test_grep_hint_present_iff_greppable(self, catalog: tuple[AntiPatternEntry, ...]) -> None:
        for entry in catalog:
            if entry.detection == "greppable":
                assert entry.grep_hint, f"{entry.id}: greppable entry missing grep_hint"
            else:
                assert entry.grep_hint is None, f"{entry.id}: judgement entry must not carry a grep_hint"

    def test_no_two_entries_share_a_grep_hint(self, catalog: tuple[AntiPatternEntry, ...]) -> None:
        hints = [e.grep_hint for e in catalog if e.grep_hint is not None]
        assert len(hints) == len(set(hints))

    def test_waivers_only_on_judgement_entries(self, catalog: tuple[AntiPatternEntry, ...]) -> None:
        for entry in catalog:
            if entry.waivers:
                assert entry.detection == "judgement", (
                    f"{entry.id}: waivers are only meaningful on a judgement entry"
                )

    def test_float_for_money_carries_the_telemetry_waiver(
        self, catalog: tuple[AntiPatternEntry, ...]
    ) -> None:
        by_id = {e.id: e for e in catalog}
        entry = by_id["float-for-money"]
        assert entry.waivers, "float-for-money must record its accepted telemetry waiver"
        joined = " ".join(entry.waivers)
        assert "provider-cost telemetry" in joined
        assert "TaskAttempt.cost_usd" in joined

    def test_every_grep_hint_compiles(self, catalog: tuple[AntiPatternEntry, ...]) -> None:
        for entry in catalog:
            if entry.grep_hint is not None:
                re.compile(entry.grep_hint)


class TestReachabilityLedger:
    def test_named_linter_resolves_to_a_real_hook_or_tool(self, catalog: tuple[AntiPatternEntry, ...]) -> None:
        for entry in catalog:
            if entry.linter is None:
                continue
            if entry.linter in _EXTERNAL_LINTERS:
                continue
            hook = _HOOKS_DIR / f"{entry.linter}.py"
            assert hook.is_file(), (
                f"{entry.id}: linter {entry.linter!r} resolves to no scripts/hooks/*.py "
                f"and is not a known external tool ({sorted(_EXTERNAL_LINTERS)})"
            )

    def test_eval_invariant_resolves_to_a_real_transcript_invariant(
        self, catalog: tuple[AntiPatternEntry, ...]
    ) -> None:
        for entry in catalog:
            if entry.eval_invariant is None:
                continue
            assert entry.eval_invariant in _KNOWN_INVARIANT_IDS, (
                f"{entry.id}: eval_invariant {entry.eval_invariant!r} is not a known "
                f"transcript_conformance invariant id ({sorted(_KNOWN_INVARIANT_IDS)})"
            )


class TestLoaderValidation:
    def _load(self, tmp_path: Path, body: str) -> tuple[AntiPatternEntry, ...]:
        path = tmp_path / "antipatterns.yaml"
        path.write_text(body, encoding="utf-8")
        return load_catalog(path)

    def test_real_catalog_loads(self) -> None:
        assert catalog_path().is_file()
        assert load_catalog()

    def test_greppable_without_grep_hint_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  severity: low\n  detection: greppable\n"
            "  anti_pattern: a\n  preferred_pattern: p\n  consumers: [linter]\n"
        )
        with pytest.raises(CatalogError, match="requires a non-empty grep_hint"):
            self._load(tmp_path, body)

    def test_judgement_with_grep_hint_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  severity: low\n  detection: judgement\n"
            "  grep_hint: foo\n  anti_pattern: a\n  preferred_pattern: p\n  consumers: [eval]\n"
        )
        with pytest.raises(CatalogError, match="forbidden on a judgement entry"):
            self._load(tmp_path, body)

    def test_duplicate_id_rejected(self, tmp_path: Path) -> None:
        one = (
            "- id: dup\n  name: X\n  severity: low\n  detection: judgement\n"
            "  anti_pattern: a\n  preferred_pattern: p\n  consumers: [eval]\n"
        )
        with pytest.raises(CatalogError, match="duplicate id"):
            self._load(tmp_path, one + one)

    def test_duplicate_grep_hint_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: a\n  name: A\n  severity: low\n  detection: greppable\n"
            "  grep_hint: foo\n  anti_pattern: a\n  preferred_pattern: p\n  consumers: [linter]\n"
            "- id: b\n  name: B\n  severity: low\n  detection: greppable\n"
            "  grep_hint: foo\n  anti_pattern: a\n  preferred_pattern: p\n  consumers: [linter]\n"
        )
        with pytest.raises(CatalogError, match="duplicates the one on entry"):
            self._load(tmp_path, body)

    def test_unknown_consumer_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  severity: low\n  detection: judgement\n"
            "  anti_pattern: a\n  preferred_pattern: p\n  consumers: [nope]\n"
        )
        with pytest.raises(CatalogError, match="unknown consumer"):
            self._load(tmp_path, body)

    def test_bad_severity_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  severity: critical\n  detection: judgement\n"
            "  anti_pattern: a\n  preferred_pattern: p\n  consumers: [eval]\n"
        )
        with pytest.raises(CatalogError, match="severity must be one of"):
            self._load(tmp_path, body)

    def test_non_kebab_id_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: Not_Kebab\n  name: X\n  severity: low\n  detection: judgement\n"
            "  anti_pattern: a\n  preferred_pattern: p\n  consumers: [eval]\n"
        )
        with pytest.raises(CatalogError, match="kebab slug"):
            self._load(tmp_path, body)

    def test_invalid_grep_hint_regex_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  severity: low\n  detection: greppable\n"
            "  grep_hint: '('\n  anti_pattern: a\n  preferred_pattern: p\n  consumers: [linter]\n"
        )
        with pytest.raises(CatalogError, match="not a valid regex"):
            self._load(tmp_path, body)

    def test_waiver_on_judgement_entry_loads(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  severity: low\n  detection: judgement\n"
            "  anti_pattern: a\n  preferred_pattern: p\n  consumers: [ac-reviewing-codebase]\n"
            "  waivers:\n    - An accepted, examined exception.\n"
        )
        (entry,) = self._load(tmp_path, body)
        assert entry.waivers == ("An accepted, examined exception.",)

    def test_waiver_defaults_to_empty_when_absent(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  severity: low\n  detection: judgement\n"
            "  anti_pattern: a\n  preferred_pattern: p\n  consumers: [eval]\n"
        )
        (entry,) = self._load(tmp_path, body)
        assert entry.waivers == ()

    def test_waiver_on_greppable_entry_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  severity: low\n  detection: greppable\n"
            "  grep_hint: foo\n  anti_pattern: a\n  preferred_pattern: p\n  consumers: [linter]\n"
            "  waivers:\n    - An exception that has no place on a mechanized entry.\n"
        )
        with pytest.raises(CatalogError, match="waivers are only allowed on a judgement entry"):
            self._load(tmp_path, body)

    def test_empty_waiver_string_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  severity: low\n  detection: judgement\n"
            "  anti_pattern: a\n  preferred_pattern: p\n  consumers: [eval]\n"
            "  waivers:\n    - '  '\n"
        )
        with pytest.raises(CatalogError, match="waivers must be a list of non-empty strings"):
            self._load(tmp_path, body)
