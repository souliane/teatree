"""The registry a gate loaded is compared with the digests committed in ``.github/term-source-fingerprint.json``."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from teatree.hooks.term_fingerprint import FingerprintUnreadableError, gate_fingerprints, unverified_gates

DRIFT_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "term_source_drift.py"
#: Mixed case, padding and a duplicate: none of it may change the digest.
REGISTRY = {"leak": ["Alpha-Term", " beta-term ", "alpha-term"], "overlay": ["gamma-term"], "tone": ["delta-term"]}


def _document(registry: dict[str, list[str]]) -> dict[str, object]:
    return {"version": 1, "gates": gate_fingerprints(registry)}


def _commit(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "term-source-fingerprint.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _seed_store(tmp_path: Path, registry: dict[str, list[str]]) -> Path:
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_term_registry', ?)",
            (json.dumps(registry),),
        )
        conn.commit()
    finally:
        conn.close()
    return db


class TestTheDigestIsTheDriftScriptsDigest:
    def test_the_document_equals_the_one_the_drift_script_writes(self, tmp_path: Path) -> None:
        out = tmp_path / "from-the-script.json"
        subprocess.run(
            [
                sys.executable,
                str(DRIFT_SCRIPT),
                "generate",
                "--db",
                str(_seed_store(tmp_path, REGISTRY)),
                "--out",
                str(out),
            ],
            check=True,
            capture_output=True,
        )

        written = json.loads(out.read_text(encoding="utf-8"))

        assert gate_fingerprints(REGISTRY) == written["gates"]

    def test_only_the_ci_fed_classes_are_fingerprinted(self) -> None:
        assert set(gate_fingerprints(REGISTRY)) == {"tree", "overlay", "allow"}


class TestUnverifiedGates:
    def test_a_registry_matching_the_committed_file_has_no_unverified_gate(self, tmp_path: Path) -> None:
        committed = _commit(tmp_path, _document(REGISTRY))

        assert unverified_gates(REGISTRY, committed) == []

    def test_reordering_recasing_and_duplicating_terms_is_not_a_difference(self, tmp_path: Path) -> None:
        committed = _commit(tmp_path, _document(REGISTRY))
        reshuffled = {"leak": ["BETA-TERM", "alpha-term"], "overlay": ["GAMMA-TERM", "gamma-term"]}

        assert unverified_gates(reshuffled, committed) == []

    def test_a_class_outside_the_two_fed_ones_is_not_compared(self, tmp_path: Path) -> None:
        committed = _commit(tmp_path, _document(REGISTRY))

        assert unverified_gates({**REGISTRY, "tone": ["another-term"]}, committed) == []

    @pytest.mark.parametrize(
        ("changed", "gates"),
        [
            ({"leak": ["something-else"]}, ["tree"]),
            ({"overlay": []}, ["overlay"]),
            ({"leak": [], "overlay": ["something-else"]}, ["overlay", "tree"]),
            ({"leak": ["other-one", "other-two"]}, ["tree"]),
            ({"overlay": ["other-three"]}, ["overlay"]),
        ],
        ids=["leak", "overlay", "both", "leak-same-size", "overlay-same-size"],
    )
    def test_a_differing_list_names_the_gate_it_belongs_to(
        self, tmp_path: Path, changed: dict[str, list[str]], gates: list[str]
    ) -> None:
        committed = _commit(tmp_path, _document(REGISTRY))

        assert sorted(unverified_gates({**REGISTRY, **changed}, committed)) == gates

    def test_a_registry_without_a_fed_class_differs_from_one_that_has_terms(self, tmp_path: Path) -> None:
        committed = _commit(tmp_path, _document(REGISTRY))

        assert sorted(unverified_gates({}, committed)) == ["overlay", "tree"]


class TestAFingerprintThatCannotBeReadIsNeverAMatch:
    def test_a_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FingerprintUnreadableError):
            unverified_gates(REGISTRY, tmp_path / "absent.json")

    def test_a_file_that_is_not_json(self, tmp_path: Path) -> None:
        path = tmp_path / "term-source-fingerprint.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(FingerprintUnreadableError):
            unverified_gates(REGISTRY, path)

    @pytest.mark.parametrize(
        "document",
        [[], {"version": 1}, {"gates": []}, {"gates": {"tree": {"count": 1, "digest": "sha256:aa"}}}],
        ids=["not-a-table", "no-gates", "gates-not-a-table", "overlay-gate-absent"],
    )
    def test_a_document_without_both_gates(self, tmp_path: Path, document: object) -> None:
        with pytest.raises(FingerprintUnreadableError):
            unverified_gates(REGISTRY, _commit(tmp_path, document))

    def test_an_entry_without_a_digest(self, tmp_path: Path) -> None:
        entry = {"count": 1}
        with pytest.raises(FingerprintUnreadableError):
            unverified_gates(REGISTRY, _commit(tmp_path, {"gates": {"tree": entry, "overlay": entry}}))

    def test_the_refusal_quotes_neither_a_digest_nor_a_term(self, tmp_path: Path) -> None:
        stored = json.dumps(_document(REGISTRY))
        path = _commit(tmp_path, {"gates": {"tree": {"count": "many", "digest": "sha256:stored-digest"}}})

        with pytest.raises(FingerprintUnreadableError) as raised:
            unverified_gates(REGISTRY, path)

        message = str(raised.value)
        assert "sha256" not in message
        assert "stored-digest" not in message
        assert not any(term in message for term in ("alpha-term", "beta-term", "gamma-term"))
        assert stored not in message


def test_the_committed_file_in_this_repository_has_every_gate() -> None:
    committed = Path(__file__).resolve().parents[2] / ".github" / "term-source-fingerprint.json"

    assert set(json.loads(committed.read_text(encoding="utf-8"))["gates"]) == {"tree", "overlay", "allow"}
