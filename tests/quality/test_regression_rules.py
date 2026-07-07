"""Conformance ledger for the named regression-detector layer (#126).

Sibling of ``test_catalog.py``. The blocking-set zero-findings assertion is what
lets the blocking gate be trusted and keeps main green: a blocking rule whose bug
re-appears on the tree turns this test red. Warn rules are advisory and must each
name a tracking issue (the flip protocol's forward guarantee).

The detector engine is ast-grep (souliane/teatree#87 migrated it off semgrep).
The engine-invoking tests need the pinned engine on PATH (``uvx`` in CI, a local
``ast-grep``/``sg`` otherwise); they skip when neither is available so a
network-less inner loop is not blocked, while CI always runs them.

Each blocking rule also carries a fires-on-bad / passes-on-good fixture pair
(``TestEachRuleFiresOnItsTarget``): the rule MUST flag its crafted defect and
MUST stay silent on the guarded-good variant. A rule that fires on neither (or on
both) guards nothing — the pair pins behavioural equivalence with the semgrep
rule it replaced.
"""

import subprocess
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from teatree.quality import regression_scan
from teatree.quality.regression_catalog import (
    BLOCKING_NOW,
    RegressionCatalogError,
    RegressionRule,
    load_astgrep_rule_ids,
    load_manifest,
    manifest_path,
    repo_root,
)
from teatree.quality.regression_scan import AstGrepUnavailableError, astgrep_invocable, scan_findings


def _fake_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _patch_which(*present: str):
    return patch.object(
        regression_scan.shutil, "which", side_effect=lambda name: f"/bin/{name}" if name in present else None
    )


requires_astgrep = pytest.mark.skipif(not astgrep_invocable(), reason="ast-grep/uvx not on PATH")


@pytest.fixture(scope="module")
def manifest() -> tuple[RegressionRule, ...]:
    return load_manifest()


@pytest.fixture(scope="module")
def blocking_dir() -> Path:
    return repo_root() / ".ast-grep" / "blocking"


class TestManifestSchema:
    def test_manifest_is_non_empty(self, manifest: tuple[RegressionRule, ...]) -> None:
        assert manifest

    def test_ids_are_unique(self, manifest: tuple[RegressionRule, ...]) -> None:
        ids = [rule.id for rule in manifest]
        assert len(ids) == len(set(ids))

    def test_every_rule_file_exists(self, manifest: tuple[RegressionRule, ...]) -> None:
        for rule in manifest:
            assert rule.rule_path.is_file(), f"{rule.id}: rule file {rule.file} does not exist"

    def test_every_rule_file_parses_as_astgrep_yaml(self, manifest: tuple[RegressionRule, ...]) -> None:
        for rule in manifest:
            ids = load_astgrep_rule_ids(rule.rule_path)
            assert ids, f"{rule.id}: rule file has no ast-grep rule id"

    def test_manifest_id_matches_the_astgrep_rule_id(self, manifest: tuple[RegressionRule, ...]) -> None:
        for rule in manifest:
            ids = load_astgrep_rule_ids(rule.rule_path)
            assert ids == (rule.id,), f"{rule.id}: rule file declares ast-grep ids {ids}, expected ({rule.id!r},)"

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
    def test_every_astgrep_rule_file_is_in_the_manifest(self, manifest: tuple[RegressionRule, ...]) -> None:
        on_disk = {
            p.relative_to(repo_root()).as_posix()
            for p in (repo_root() / ".ast-grep").rglob("*.yml")
            if p.name != "sgconfig.yml"
        }
        declared = {rule.file for rule in manifest}
        assert on_disk == declared, (
            f"manifest/disk drift: only-disk={on_disk - declared} only-manifest={declared - on_disk}"
        )


@pytest.mark.push_heavy
class TestBlockingSetIsGreen:
    # The whole-tree ast-grep scan (>300s tail) is RELOCATED off the push gate to
    # CI's test-shard lane (#122, mirroring #3032's jscpd/mutmut relocation). At
    # push the scoped Engine B in `dev/push-gate.sh` covers the changed files; CI's
    # shard lane runs this whole-tree scan on every PR (no marker filter).
    @pytest.mark.timeout(300)
    @requires_astgrep
    def test_blocking_rules_have_zero_findings_on_the_current_tree(self, blocking_dir: Path) -> None:
        findings = scan_findings(blocking_dir)
        assert findings == [], (
            "blocking regression rules must be zero-findings on the current tree (main stays green); "
            f"got: {[(f['check_id'], f['path'], f['start']['line']) for f in findings]}"
        )


class TestEachRuleFiresOnItsTarget:
    """Anti-vacuous proof that each migrated rule preserves its semgrep behaviour.

    Each entry pairs a *bad* fixture (the defect the rule must flag) with a *good*
    fixture (the guarded variant it must pass). The fixtures are written under the
    rule's own scoped path so the rule-level ``files`` glob selects them. Each
    fixture is a single Python file; the rule fires (>=1 finding) on bad and stays
    silent (0 findings) on good.
    """

    _CASES: ClassVar[dict[str, dict[str, str]]] = {
        "predictable-temp-path": {
            "path": "src/teatree/core/target.py",
            "bad": ('from pathlib import Path\n\n\ndef f(s):\n    Path(f"/tmp/run-{s}.json").write_text("x")\n'),
            "good": (
                "import tempfile\nfrom pathlib import Path\n\n\n"
                "def f():\n"
                '    Path(tempfile.mkstemp()[1]).write_text("x")\n'
            ),
        },
        "consume-before-side-effect-not-atomic": {
            "path": "src/teatree/core/on_behalf_gate_recorded.py",
            "bad": (
                "def gate(target, action, publish):\n"
                "    consumed = OnBehalfApproval.consume(target, action)\n"
                "    result = publish()\n"
                "    OnBehalfAudit.objects.create(approval=consumed)\n"
                "    return result\n"
            ),
            "good": (
                "def gate(target, action, publish):\n"
                "    with transaction.atomic():\n"
                "        consumed = OnBehalfApproval.consume(target, action)\n"
                "        result = publish()\n"
                "        OnBehalfAudit.objects.create(approval=consumed)\n"
                "        return result\n"
            ),
        },
        "cas-stamp-before-side-effect": {
            "path": "src/teatree/loop/slack_answer/cycle.py",
            "bad": (
                "def react_once(backend, row):\n"
                "    if row.eyes_reacted_at is not None or not row.mark_eyes_reacted():\n"
                "        return\n"
                '    backend.react(channel=row.channel, ts=row.slack_ts, emoji="eyes")\n'
            ),
            "good": (
                "def react_once(backend, row):\n"
                "    if row.eyes_reacted_at is not None or not row.mark_eyes_reacted():\n"
                "        return\n"
                "    try:\n"
                '        backend.react(channel=row.channel, ts=row.slack_ts, emoji="eyes")\n'
                "    except Exception:\n"
                "        row.unmark_eyes_reacted()\n"
                "        raise\n"
            ),
        },
        "response-json-on-2xx-uncaught": {
            "path": "src/teatree/backends/slack/reactions.py",
            "bad": ("def parse(response):\n    payload = response.json()\n    return payload\n"),
            "good": (
                "import json\n\n\n"
                "def parse(response):\n"
                "    try:\n"
                "        return response.json()\n"
                "    except (json.JSONDecodeError, ValueError):\n"
                "        return None\n"
            ),
        },
        "signal-receiver-without-fault-isolation": {
            "path": "src/teatree/core/signals.py",
            "bad": (
                "def _record(sender, instance, name, **kw):\n"
                "    TicketTransition.objects.create(ticket=instance, name=name)\n"
            ),
            "good": (
                "def _record(sender, instance, name, **kw):\n"
                "    try:\n"
                "        TicketTransition.objects.create(ticket=instance, name=name)\n"
                "    except Exception:\n"
                '        logger.exception("failed")\n'
            ),
        },
        "except-swallow-to-empty": {
            "path": "src/teatree/core/overlay.py",
            "bad": (
                'def get_issue_title():\n    try:\n        return fetch()\n    except Exception:\n        return ""\n'
            ),
            "good": (
                "def get_issue_title():\n"
                "    try:\n"
                "        return fetch()\n"
                "    except Exception:\n"
                '        logger.warning("fetch failed")\n'
                '        return ""\n'
            ),
        },
        "gate-conflates-scanner-error-with-finding": {
            "path": "hooks/scripts/hook_router.py",
            "bad": (
                "def check(result):\n"
                "    if result.returncode != 0:\n"
                '        return emit_pretooluse_deny("banned trailer in the PR body or commit message", extra=1)\n'
                "    return False\n"
            ),
            "good": (
                "def check(result):\n"
                "    if _ai_sig_finding(result.stdout):\n"
                '        return emit_pretooluse_deny("banned trailer in the PR body or commit message", extra=1)\n'
                "    if result.returncode != 0:\n"
                '        return emit_pretooluse_deny("scanner error, not a finding", extra=1)\n'
                "    return False\n"
            ),
        },
    }

    def _scan_fixture(self, rule: RegressionRule, tmp_path: Path, source: str) -> list[dict]:
        case = self._CASES[rule.id]
        fixture = tmp_path / case["path"]
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(source, encoding="utf-8")
        sgconfig = tmp_path / ".ast-grep" / "sgconfig.yml"
        sgconfig.parent.mkdir(parents=True, exist_ok=True)
        sgconfig.write_text("ruleDirs:\n  - blocking\n", encoding="utf-8")
        blocking = tmp_path / ".ast-grep" / "blocking"
        blocking.mkdir(parents=True, exist_ok=True)
        (blocking / rule.rule_path.name).write_text(rule.rule_path.read_text(encoding="utf-8"), encoding="utf-8")
        return scan_findings(blocking, root=tmp_path)

    @pytest.mark.timeout(300)
    @requires_astgrep
    @pytest.mark.parametrize("rule_id", sorted(_CASES))
    def test_rule_fires_on_its_bad_fixture(self, rule_id: str, tmp_path: Path) -> None:
        rule = next(r for r in load_manifest() if r.id == rule_id)
        findings = self._scan_fixture(rule, tmp_path, self._CASES[rule_id]["bad"])
        assert any(f["check_id"] == rule_id for f in findings), (
            f"{rule_id}: rule did not fire on its crafted bad fixture (guards nothing); findings={findings}"
        )

    @pytest.mark.timeout(300)
    @requires_astgrep
    @pytest.mark.parametrize("rule_id", sorted(_CASES))
    def test_rule_passes_on_its_good_fixture(self, rule_id: str, tmp_path: Path) -> None:
        rule = next(r for r in load_manifest() if r.id == rule_id)
        findings = self._scan_fixture(rule, tmp_path, self._CASES[rule_id]["good"])
        assert not any(f["check_id"] == rule_id for f in findings), (
            f"{rule_id}: rule fired on its guarded-good fixture (false positive); findings={findings}"
        )

    def test_every_blocking_rule_has_a_fixture_case(self) -> None:
        blocking_ids = {r.id for r in load_manifest() if r.is_blocking}
        assert blocking_ids <= set(self._CASES), (
            f"blocking rules with no fires-on-target fixture: {blocking_ids - set(self._CASES)}"
        )


class TestAstGrepEngine:
    @pytest.mark.timeout(300)
    def test_engine_is_invocable(self) -> None:
        assert astgrep_invocable(), (
            "the pinned ast-grep engine is not invocable; a future bump or a broken pin must fail HERE, "
            "not silently no-op the CI scan"
        )


class TestAstGrepEngineBranches:
    def test_prefers_uvx_pinned_runner(self) -> None:
        with _patch_which("uvx", "ast-grep"):
            argv = regression_scan._astgrep_argv()
            assert argv[0] == "uvx"
            assert f"ast-grep-cli=={regression_scan.ASTGREP_PIN}" in argv

    def test_falls_back_to_system_ast_grep(self) -> None:
        with _patch_which("ast-grep"):
            assert regression_scan._astgrep_argv() == ["ast-grep"]

    def test_falls_back_to_sg_binary(self) -> None:
        with _patch_which("sg"):
            assert regression_scan._astgrep_argv() == ["sg"]

    def test_raises_when_no_engine_present(self) -> None:
        with _patch_which(), pytest.raises(AstGrepUnavailableError, match="neither"):
            regression_scan._astgrep_argv()

    def test_not_invocable_when_no_engine(self) -> None:
        with _patch_which():
            assert astgrep_invocable() is False

    def test_invocable_reads_version_exit_code(self) -> None:
        with (
            _patch_which("ast-grep"),
            patch.object(regression_scan, "run_allowed_to_fail", return_value=_fake_completed(0)),
        ):
            assert astgrep_invocable() is True

    def test_scan_findings_normalizes_results(self, tmp_path: Path) -> None:
        (tmp_path / "blocking").mkdir()
        (tmp_path / "blocking" / "x.yml").write_text("id: x\nlanguage: python\nrule:\n  pattern: y\n", encoding="utf-8")
        (tmp_path / "sgconfig.yml").write_text("ruleDirs:\n  - blocking\n", encoding="utf-8")
        payload = '[{"ruleId": "x", "file": "a.py", "range": {"start": {"line": 7, "column": 0}}}]'
        with (
            _patch_which("ast-grep"),
            patch.object(regression_scan, "run_allowed_to_fail", return_value=_fake_completed(0, stdout=payload)),
        ):
            findings = scan_findings(tmp_path / "blocking")
        assert findings == [{"check_id": "x", "path": "a.py", "start": {"line": 7}}]

    def test_scan_findings_raises_on_empty_output(self, tmp_path: Path) -> None:
        (tmp_path / "blocking").mkdir()
        (tmp_path / "blocking" / "x.yml").write_text("id: x\nlanguage: python\nrule:\n  pattern: y\n", encoding="utf-8")
        (tmp_path / "sgconfig.yml").write_text("ruleDirs:\n  - blocking\n", encoding="utf-8")
        with (
            _patch_which("ast-grep"),
            patch.object(regression_scan, "run_allowed_to_fail", return_value=_fake_completed(2, stderr="boom")),
            pytest.raises(AstGrepUnavailableError, match="no JSON output"),
        ):
            scan_findings(tmp_path / "blocking")

    def test_scan_findings_raises_when_no_rules(self, tmp_path: Path) -> None:
        (tmp_path / "blocking").mkdir()
        with _patch_which("ast-grep"), pytest.raises(AstGrepUnavailableError, match="no ast-grep rules"):
            scan_findings(tmp_path / "blocking")


class TestScanFindingsPathScoping:
    """``paths=`` scopes Engine B to the changed files; ``None`` stays whole-tree (#122)."""

    def _rules_dir(self, tmp_path: Path) -> Path:
        (tmp_path / "blocking").mkdir()
        (tmp_path / "blocking" / "x.yml").write_text("id: x\nlanguage: python\nrule:\n  pattern: y\n", encoding="utf-8")
        (tmp_path / "sgconfig.yml").write_text("ruleDirs:\n  - blocking\n", encoding="utf-8")
        return tmp_path / "blocking"

    def test_none_paths_scans_whole_tree_no_positional_files(self, tmp_path: Path) -> None:
        rules = self._rules_dir(tmp_path)
        with (
            _patch_which("ast-grep"),
            patch.object(regression_scan, "run_allowed_to_fail", return_value=_fake_completed(0, stdout="[]")) as run,
        ):
            scan_findings(rules)
        cmd = run.call_args.args[0]
        assert cmd[-1] == "--json", "whole-tree scan must not append positional file args"

    def test_scoped_paths_are_appended_as_positional_args(self, tmp_path: Path) -> None:
        rules = self._rules_dir(tmp_path)
        scope = [Path("src/teatree/core/session.py"), Path("tests/teatree_core/test_session.py")]
        with (
            _patch_which("ast-grep"),
            patch.object(regression_scan, "run_allowed_to_fail", return_value=_fake_completed(0, stdout="[]")) as run,
        ):
            scan_findings(rules, paths=scope)
        cmd = run.call_args.args[0]
        assert cmd[-2:] == [str(p) for p in scope], "scoped scan must append exactly the changed files"

    def test_empty_paths_returns_no_findings_without_invoking_astgrep(self, tmp_path: Path) -> None:
        # An empty positional list would make ast-grep scan the WHOLE tree — the
        # opposite of "no files in scope" — so an empty scope must short-circuit.
        rules = self._rules_dir(tmp_path)
        with (
            _patch_which("ast-grep"),
            patch.object(regression_scan, "run_allowed_to_fail") as run,
        ):
            findings = scan_findings(rules, paths=[])
        assert findings == []
        run.assert_not_called()


class TestLoaderValidation:
    def _load(self, tmp_path: Path, body: str) -> tuple[RegressionRule, ...]:
        path = tmp_path / "regression_rules.yaml"
        path.write_text(body, encoding="utf-8")
        return load_manifest(path)

    def test_real_manifest_loads(self) -> None:
        assert manifest_path().is_file()
        assert load_manifest()

    def test_non_kebab_id_rejected(self, tmp_path: Path) -> None:
        body = "- id: Not_Kebab\n  issue: BLOCKING-NOW\n  status: blocking\n  file: .ast-grep/blocking/Not_Kebab.yml\n"
        with pytest.raises(RegressionCatalogError, match="kebab slug"):
            self._load(tmp_path, body)

    def test_bad_status_rejected(self, tmp_path: Path) -> None:
        body = "- id: x\n  issue: BLOCKING-NOW\n  status: nope\n  file: .ast-grep/nope/x.yml\n"
        with pytest.raises(RegressionCatalogError, match="status must be one of"):
            self._load(tmp_path, body)

    def test_blocking_with_tracking_issue_rejected(self, tmp_path: Path) -> None:
        body = "- id: x\n  issue: souliane/teatree#1\n  status: blocking\n  file: .ast-grep/blocking/x.yml\n"
        with pytest.raises(RegressionCatalogError, match="blocking rule's issue must be"):
            self._load(tmp_path, body)

    def test_warn_without_tracking_issue_rejected(self, tmp_path: Path) -> None:
        body = "- id: x\n  issue: BLOCKING-NOW\n  status: warn\n  file: .ast-grep/warn/x.yml\n"
        with pytest.raises(RegressionCatalogError, match="must name a souliane/teatree"):
            self._load(tmp_path, body)

    def test_file_in_wrong_dir_rejected(self, tmp_path: Path) -> None:
        body = "- id: x\n  issue: BLOCKING-NOW\n  status: blocking\n  file: .ast-grep/warn/x.yml\n"
        with pytest.raises(RegressionCatalogError, match=r"must live under \.ast-grep/blocking/"):
            self._load(tmp_path, body)

    def test_file_name_id_mismatch_rejected(self, tmp_path: Path) -> None:
        body = "- id: x\n  issue: BLOCKING-NOW\n  status: blocking\n  file: .ast-grep/blocking/y.yml\n"
        with pytest.raises(RegressionCatalogError, match="must match the entry id"):
            self._load(tmp_path, body)

    def test_duplicate_id_rejected(self, tmp_path: Path) -> None:
        one = "- id: dup\n  issue: BLOCKING-NOW\n  status: blocking\n  file: .ast-grep/blocking/dup.yml\n"
        with pytest.raises(RegressionCatalogError, match="duplicate id"):
            self._load(tmp_path, one + one)

    def test_invalid_astgrep_file_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yml"
        bad.write_text("not_rule: []\n", encoding="utf-8")
        with pytest.raises(RegressionCatalogError, match="must be a mapping with a 'rule' block"):
            load_astgrep_rule_ids(bad)

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
            self._load(tmp_path, "- id: x\n  status: blocking\n  file: .ast-grep/blocking/x.yml\n")

    def test_malformed_astgrep_yaml_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yml"
        bad.write_text("rule: : :\n", encoding="utf-8")
        with pytest.raises(RegressionCatalogError, match="invalid ast-grep YAML"):
            load_astgrep_rule_ids(bad)

    def test_astgrep_rule_without_string_id_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yml"
        bad.write_text("rule:\n  pattern: x\n", encoding="utf-8")
        with pytest.raises(RegressionCatalogError, match="needs a string 'id'"):
            load_astgrep_rule_ids(bad)
