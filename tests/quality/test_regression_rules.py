"""Conformance ledger for the named regression-detector layer (#126).

Sibling of ``test_catalog.py``. The blocking-set zero-findings assertion is what
lets the blocking gate be trusted and keeps main green: a blocking rule whose bug
re-appears on the tree turns this test red. Warn rules are advisory and must each
name a tracking issue (the flip protocol's forward guarantee).

The semgrep-invoking tests need the pinned engine on PATH (``uvx`` in CI, a local
``semgrep``/``uvx`` otherwise); they skip when neither is available so a
network-less inner loop is not blocked, while CI always runs them.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.quality import regression_scan
from teatree.quality.regression_catalog import (
    BLOCKING_NOW,
    RegressionCatalogError,
    RegressionRule,
    load_manifest,
    load_semgrep_rule_ids,
    manifest_path,
    repo_root,
)
from teatree.quality.regression_scan import SemgrepUnavailableError, scan_findings, semgrep_invocable


def _fake_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _patch_which(*present: str):
    return patch.object(
        regression_scan.shutil, "which", side_effect=lambda name: f"/bin/{name}" if name in present else None
    )


requires_semgrep = pytest.mark.skipif(not semgrep_invocable(), reason="semgrep/uvx not on PATH")


@pytest.fixture(scope="module")
def manifest() -> tuple[RegressionRule, ...]:
    return load_manifest()


@pytest.fixture(scope="module")
def blocking_dir() -> Path:
    return repo_root() / ".semgrep" / "blocking"


class TestManifestSchema:
    def test_manifest_is_non_empty(self, manifest: tuple[RegressionRule, ...]) -> None:
        assert manifest

    def test_ids_are_unique(self, manifest: tuple[RegressionRule, ...]) -> None:
        ids = [rule.id for rule in manifest]
        assert len(ids) == len(set(ids))

    def test_every_rule_file_exists(self, manifest: tuple[RegressionRule, ...]) -> None:
        for rule in manifest:
            assert rule.rule_path.is_file(), f"{rule.id}: rule file {rule.file} does not exist"

    def test_every_rule_file_parses_as_semgrep_yaml(self, manifest: tuple[RegressionRule, ...]) -> None:
        for rule in manifest:
            ids = load_semgrep_rule_ids(rule.rule_path)
            assert ids, f"{rule.id}: rule file has no semgrep rules"

    def test_manifest_id_matches_the_semgrep_rule_id(self, manifest: tuple[RegressionRule, ...]) -> None:
        for rule in manifest:
            ids = load_semgrep_rule_ids(rule.rule_path)
            assert ids == (rule.id,), f"{rule.id}: rule file declares semgrep ids {ids}, expected ({rule.id!r},)"

    def test_blocking_rules_use_the_blocking_now_sentinel(self, manifest: tuple[RegressionRule, ...]) -> None:
        for rule in manifest:
            if rule.is_blocking:
                assert rule.issue == BLOCKING_NOW

    def test_every_warn_rule_names_a_tracking_issue(self, manifest: tuple[RegressionRule, ...]) -> None:
        for rule in manifest:
            if not rule.is_blocking:
                assert rule.issue.startswith("souliane/teatree#"), (
                    f"{rule.id}: a warn rule must name a souliane/teatree#<n> tracking issue, got {rule.issue!r}"
                )


class TestManifestMatchesTree:
    def test_every_semgrep_file_is_in_the_manifest(self, manifest: tuple[RegressionRule, ...]) -> None:
        on_disk = {p.relative_to(repo_root()).as_posix() for p in (repo_root() / ".semgrep").rglob("*.yaml")}
        declared = {rule.file for rule in manifest}
        assert on_disk == declared, (
            f"manifest/disk drift: only-disk={on_disk - declared} only-manifest={declared - on_disk}"
        )


class TestBlockingSetIsGreen:
    # A full semgrep scan of the tree runs ~35s solo but is subprocess-bound, so
    # it overruns the global 60s timeout under the parallel host load of ``-n
    # auto`` even though it does the same fixed work. Give it headroom like the
    # other subprocess-heavy integration tests rather than let load flake it.
    @pytest.mark.timeout(300)
    @requires_semgrep
    def test_blocking_rules_have_zero_findings_on_the_current_tree(self, blocking_dir: Path) -> None:
        findings = scan_findings(blocking_dir)
        assert findings == [], (
            "blocking regression rules must be zero-findings on the current tree (main stays green); "
            f"got: {[(f['check_id'], f['path'], f['start']['line']) for f in findings]}"
        )


class TestSemgrepEngine:
    def test_semgrep_is_invocable(self) -> None:
        assert semgrep_invocable(), (
            "the pinned semgrep engine is not invocable; a future bump or a broken pin must fail HERE, "
            "not silently no-op the CI scan"
        )


class TestSemgrepEngineBranches:
    def test_prefers_a_local_semgrep_binary(self) -> None:
        with _patch_which("semgrep", "uvx"):
            assert regression_scan._semgrep_argv() == ["semgrep"]

    def test_falls_back_to_uvx(self) -> None:
        with _patch_which("uvx"):
            assert regression_scan._semgrep_argv()[0] == "uvx"

    def test_raises_when_neither_engine_present(self) -> None:
        with _patch_which(), pytest.raises(SemgrepUnavailableError, match="neither"):
            regression_scan._semgrep_argv()

    def test_not_invocable_when_no_engine(self) -> None:
        with _patch_which():
            assert semgrep_invocable() is False

    def test_invocable_reads_version_exit_code(self) -> None:
        with (
            _patch_which("semgrep"),
            patch.object(regression_scan, "run_allowed_to_fail", return_value=_fake_completed(0)),
        ):
            assert semgrep_invocable() is True

    def test_scan_findings_parses_results(self, tmp_path: Path) -> None:
        payload = '{"results": [{"check_id": "x"}]}'
        with (
            _patch_which("semgrep"),
            patch.object(regression_scan, "run_allowed_to_fail", return_value=_fake_completed(1, stdout=payload)),
        ):
            assert scan_findings(tmp_path) == [{"check_id": "x"}]

    def test_scan_findings_raises_on_empty_output(self, tmp_path: Path) -> None:
        with (
            _patch_which("semgrep"),
            patch.object(regression_scan, "run_allowed_to_fail", return_value=_fake_completed(2, stderr="boom")),
            pytest.raises(SemgrepUnavailableError, match="no JSON output"),
        ):
            scan_findings(tmp_path)


class TestLoaderValidation:
    def _load(self, tmp_path: Path, body: str) -> tuple[RegressionRule, ...]:
        path = tmp_path / "regression_rules.yaml"
        path.write_text(body, encoding="utf-8")
        return load_manifest(path)

    def test_real_manifest_loads(self) -> None:
        assert manifest_path().is_file()
        assert load_manifest()

    def test_non_kebab_id_rejected(self, tmp_path: Path) -> None:
        body = "- id: Not_Kebab\n  issue: BLOCKING-NOW\n  status: blocking\n  file: .semgrep/blocking/Not_Kebab.yaml\n"
        with pytest.raises(RegressionCatalogError, match="kebab slug"):
            self._load(tmp_path, body)

    def test_bad_status_rejected(self, tmp_path: Path) -> None:
        body = "- id: x\n  issue: BLOCKING-NOW\n  status: nope\n  file: .semgrep/nope/x.yaml\n"
        with pytest.raises(RegressionCatalogError, match="status must be one of"):
            self._load(tmp_path, body)

    def test_blocking_with_tracking_issue_rejected(self, tmp_path: Path) -> None:
        body = "- id: x\n  issue: souliane/teatree#1\n  status: blocking\n  file: .semgrep/blocking/x.yaml\n"
        with pytest.raises(RegressionCatalogError, match="blocking rule's issue must be"):
            self._load(tmp_path, body)

    def test_warn_without_tracking_issue_rejected(self, tmp_path: Path) -> None:
        body = "- id: x\n  issue: BLOCKING-NOW\n  status: warn\n  file: .semgrep/warn/x.yaml\n"
        with pytest.raises(RegressionCatalogError, match="must name a souliane/teatree"):
            self._load(tmp_path, body)

    def test_file_in_wrong_dir_rejected(self, tmp_path: Path) -> None:
        body = "- id: x\n  issue: BLOCKING-NOW\n  status: blocking\n  file: .semgrep/warn/x.yaml\n"
        with pytest.raises(RegressionCatalogError, match=r"must live under \.semgrep/blocking/"):
            self._load(tmp_path, body)

    def test_file_name_id_mismatch_rejected(self, tmp_path: Path) -> None:
        body = "- id: x\n  issue: BLOCKING-NOW\n  status: blocking\n  file: .semgrep/blocking/y.yaml\n"
        with pytest.raises(RegressionCatalogError, match="must match the entry id"):
            self._load(tmp_path, body)

    def test_duplicate_id_rejected(self, tmp_path: Path) -> None:
        one = "- id: dup\n  issue: BLOCKING-NOW\n  status: blocking\n  file: .semgrep/blocking/dup.yaml\n"
        with pytest.raises(RegressionCatalogError, match="duplicate id"):
            self._load(tmp_path, one + one)

    def test_invalid_semgrep_file_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("not_rules: []\n", encoding="utf-8")
        with pytest.raises(RegressionCatalogError, match="must be a mapping with a 'rules' list"):
            load_semgrep_rule_ids(bad)

    def test_malformed_manifest_yaml_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(RegressionCatalogError):
            self._load(tmp_path, "- id: x\n   bad: : indent\n")

    def test_non_list_manifest_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(RegressionCatalogError, match="top-level YAML list"):
            self._load(tmp_path, "id: x\n")

    def test_non_mapping_entry_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(RegressionCatalogError, match="each entry must be a mapping"):
            self._load(tmp_path, "- just-a-string\n")

    def test_missing_required_field_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(RegressionCatalogError, match="required string field"):
            self._load(tmp_path, "- id: x\n  status: blocking\n  file: .semgrep/blocking/x.yaml\n")

    def test_malformed_semgrep_yaml_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("rules: : :\n", encoding="utf-8")
        with pytest.raises(RegressionCatalogError, match="invalid semgrep YAML"):
            load_semgrep_rule_ids(bad)

    def test_empty_rules_list_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("rules: []\n", encoding="utf-8")
        with pytest.raises(RegressionCatalogError, match="non-empty list"):
            load_semgrep_rule_ids(bad)

    def test_semgrep_rule_without_string_id_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("rules:\n  - message: m\n", encoding="utf-8")
        with pytest.raises(RegressionCatalogError, match="needs a string 'id'"):
            load_semgrep_rule_ids(bad)
