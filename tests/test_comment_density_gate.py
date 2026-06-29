"""Golden corpus + surface tests for the advisory comment-density check.

The check flags a diff that adds comments which merely restate the code (names +
types are the documentation). It is **advisory** — every surface prints the
finding as a warning and exits 0, so it never blocks a commit, push, or
pipeline. The load-bearing part is the **golden corpus** — two symmetric,
non-negotiable sets that pin the detection regardless of the (always-zero)
exit code.

The **must-DENY** set is diffs the check MUST flag: WHAT-narration by ratio, a
3+ consecutive-comment run, a TS block-comment run, a single inline comment
restating the next code line, and a signature-echo docstring (the content-aware
restatement detection).

The **must-ALLOW** set is clean diffs the check MUST pass: sparse code, one
explanatory comment, a docstring carrying a non-obvious why, tooling pragmas,
license/shebang headers, tests, docs, CI YAML rationale blocks. It proves the
check cannot over-flag legitimate code — a detector without it is incomplete
(the over-flag doctrine).

The check is content-aware (it flags a comment whose words restate the next
code line, and a docstring opening that echoes the signature) and diff-aware;
the analysis lives in :mod:`teatree.hooks.privacy_diff_comment_density`,
exercised here through the reusable :func:`report_diff`, the ``t3 tool
comment-density`` CLI, and the ``scripts/hooks/check_comment_density.py``
pre-push hook (each warn-only).
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.hooks.privacy_diff_comment_density import _is_pragma, report_diff

runner = CliRunner()

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "check_comment_density.py"
GIT = shutil.which("git") or "git"


@pytest.fixture(autouse=True)
def _suppress_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.cli._maybe_show_update_notice", lambda: None)


def _diff(path: str, *added: str, start: int = 10) -> str:
    body = "".join(f"+{line}\n" for line in added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -{start},0 +{start},{len(added)} @@\n{body}"


# --- Golden corpus -----------------------------------------------------------

_MUST_DENY: dict[str, str] = {
    "ratio_what_narration": _diff(
        "src/teatree/x.py",
        "def handle(value):",
        "    # increment the counter by one",
        "    # then store it in the cache",
        "    # so later reads are fast",
        "    counter += 1",
        "    cache[key] = counter",
    ),
    "scattered_ratio": _diff(
        "src/teatree/x.py",
        "a = 1",
        "    # narrate the first step",
        "b = 2",
        "    # narrate the second step",
        "c = 3",
        "    # narrate the third step",
        "d = 4",
    ),
    "consecutive_run": _diff(
        "src/teatree/x.py",
        "x = compute(a)",
        "y = compute(b)",
        "z = compute(c)",
        "w = compute(d)",
        "v = compute(e)",
        "    # first the inputs are normalised",
        "    # then the totals are summed",
        "    # finally the result is rounded",
        "final = round(v)",
    ),
    "ts_block_comment_run": _diff(
        "src/app/widget.ts",
        "const total = a + b + c + d;",
        "const ratio = total / count;",
        "const scaled = ratio * factor;",
        "const clamped = Math.min(scaled, max);",
        "const result = Math.max(clamped, min);",
        "  // normalise the raw inputs first",
        "  // then apply the scaling factor",
        "  // and clamp into the valid range",
        "return result;",
    ),
    "migration_signature_echo_docstring": _diff(
        "src/myapp/migrations/0002_add_flag.py",
        "def add_feature_flag(apps, schema_editor):",
        '    """Add the feature flag."""',
        "    model = apps.get_model('myapp', 'FeatureFlag')",
        "    model.objects.get_or_create(definition=FLAG, defaults={'value': False})",
    ),
    "single_inline_comment_restating_the_update_call": _diff(
        "src/myapp/migrations/0002_add_flag.py",
        "def backfill(model, metadata):",
        "    # update the rows with the metadata",
        "    model.objects.filter(definition=FLAG).update(**metadata)",
    ),
}

_MUST_ALLOW: dict[str, str] = {
    "sparse_code": _diff(
        "src/teatree/x.py",
        "def handle(value):",
        "    counter = value + 1",
        "    cache[key] = counter",
        "    return counter",
    ),
    "one_explanatory_comment": _diff(
        "src/teatree/x.py",
        "def handle(value):",
        "    # SandboxedEnvironment blocks attribute-access SSTI",
        "    env = SandboxedEnvironment()",
        "    template = env.from_string(value)",
        "    return template.render()",
    ),
    "docstring_body": _diff(
        "src/teatree/x.py",
        "def handle(value):",
        '    """Normalise and persist the value.',
        "",
        "    The value is normalised then written through to the cache",
        "    so subsequent reads stay fast under load.",
        '    """',
        "    counter = value + 1",
        "    cache[key] = counter",
        "    return counter",
    ),
    "python_pragmas": _diff(
        "src/teatree/x.py",
        "def handle(value):",
        "    # type: ignore[assignment]",
        "    # noqa: E501",
        "    # pragma: no cover",
        "    return legacy(value)",
    ),
    "eslint_and_ts_pragmas": _diff(
        "src/app/x.ts",
        "const a = compute();",
        "  // eslint-disable-next-line no-console",
        "  // eslint-disable-next-line no-unused-vars",
        "  // @ts-expect-error legacy shim",
        "const b = run(a);",
    ),
    "security_rationale": _diff(
        "src/teatree/x.py",
        "def handle(value):",
        "    # security: untrusted input must be validated before use",
        "    # security: the sandbox denies attribute access to block SSTI",
        "    # security: rendering happens only after the allowlist check",
        "    return render(validate(value))",
    ),
    "leading_license_header": _diff(
        "src/teatree/x.py",
        "#!/usr/bin/env python",
        "# Copyright 2026 Example",
        "# SPDX-License-Identifier: Apache-2.0",
        "",
        "import os",
        "value = os.getcwd()",
        start=1,
    ),
    "tests_path": _diff(
        "tests/test_x.py",
        "def test_handle():",
        "    # increment the counter by one",
        "    # then store it in the cache",
        "    # so later reads are fast",
        "    assert handle(1) == 2",
    ),
    "markdown_doc": _diff(
        "docs/notes.md",
        "# heading",
        "# another heading",
        "# yet another heading",
        "some prose here",
    ),
    "ci_yaml_rationale_block": _diff(
        ".github/workflows/ci.yml",
        "  some-gate:",
        "    # This job re-runs the same check on the PR diff so an agent",
        "    # that bypasses prek still trips the gate. Stages the PR-vs-base",
        "    # diff into the index so the hook's git diff --cached query",
        "    # sees the PR contents — the documented CI convention.",
        "    runs-on: ubuntu-latest",
    ),
    "migration_docstring_with_non_obvious_why": _diff(
        "src/myapp/migrations/0002_add_flag.py",
        "def add_feature_flag(apps, schema_editor):",
        '    """Seed the platform-messages flag, OFF by default.',
        "",
        "    The endpoint reports zero messages while OFF so the capability",
        "    stays dark per tenant until each environment opts in.",
        '    """',
        "    model = apps.get_model('myapp', 'FeatureFlag')",
        "    model.objects.get_or_create(definition=FLAG, defaults={'value': False})",
    ),
}


class TestGoldenCorpus:
    """The anti-vacuous proof: heavy diffs flag, clean diffs pass."""

    @pytest.mark.parametrize("name", sorted(_MUST_DENY))
    def test_must_deny_is_flagged(self, name: str) -> None:
        findings = report_diff(_MUST_DENY[name])
        assert findings, f"must-DENY corpus case {name!r} was NOT flagged"

    @pytest.mark.parametrize("name", sorted(_MUST_ALLOW))
    def test_must_allow_passes(self, name: str) -> None:
        findings = report_diff(_MUST_ALLOW[name])
        assert findings == [], f"must-ALLOW corpus case {name!r} was wrongly flagged: {findings}"


class TestPragmaExemption:
    """Tooling pragmas are machine directives, not WHAT-narration."""

    @pytest.mark.parametrize(
        "line",
        [
            "# type: ignore",
            "    # noqa: E501",
            "# pragma: no cover",
            "# pyright: ignore[reportGeneralTypeIssues]",
            "# mypy: disable-error-code=attr-defined",
            "# ruff: noqa",
            "  // eslint-disable-next-line no-console",
            "// @ts-ignore",
            "  // @ts-expect-error",
            "/* istanbul ignore next */",
            "// c8 ignore next",
            "// biome-ignore lint: legacy",
        ],
    )
    def test_pragma_lines_recognised(self, line: str) -> None:
        assert _is_pragma(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            "# increment the counter",
            "// normalise the inputs",
            "# the type of this value is int",
        ],
    )
    def test_prose_comments_are_not_pragmas(self, line: str) -> None:
        assert _is_pragma(line) is False


class TestStructuredFinding:
    """``report_diff`` carries actionable per-file counts."""

    def test_finding_exposes_counts_and_reason(self) -> None:
        findings = report_diff(_MUST_DENY["ratio_what_narration"])
        assert len(findings) == 1
        finding = findings[0]
        assert finding.path == "src/teatree/x.py"
        assert finding.comment_lines >= 3
        assert "comment-dense" in finding.render()
        assert finding.ratio > 0

    def test_zero_code_lines_ratio_is_zero(self) -> None:
        from teatree.hooks.privacy_diff_comment_density import CommentDensityFinding  # noqa: PLC0415

        finding = CommentDensityFinding(
            path="src/teatree/x.py", comment_lines=3, code_lines=0, max_consecutive=3, restatements=0, reason="x"
        )
        assert not finding.ratio

    def test_restatement_finding_reports_count_and_reason(self) -> None:
        findings = report_diff(_MUST_DENY["single_inline_comment_restating_the_update_call"])
        assert len(findings) == 1
        assert findings[0].restatements >= 1
        assert "restate" in findings[0].reason


class TestCli:
    """``t3 tool comment-density`` reads the diff and always exits 0 (advisory)."""

    def test_stdin_heavy_diff_warns_and_exits_zero(self) -> None:
        result = runner.invoke(app, ["tool", "comment-density"], input=_MUST_DENY["ratio_what_narration"])
        assert result.exit_code == 0
        assert "comment-density warning" in result.output

    def test_stdin_clean_diff_exits_zero(self) -> None:
        result = runner.invoke(app, ["tool", "comment-density"], input=_MUST_ALLOW["sparse_code"])
        assert result.exit_code == 0
        assert "no findings" in result.output

    def test_diff_file_source_warns_and_exits_zero(self, tmp_path: Path) -> None:
        diff_path = tmp_path / "change.diff"
        diff_path.write_text(_MUST_DENY["consecutive_run"], encoding="utf-8")
        result = runner.invoke(app, ["tool", "comment-density", "--diff", str(diff_path)])
        assert result.exit_code == 0
        assert "comment-density warning" in result.output

    def test_json_output(self) -> None:
        result = runner.invoke(app, ["tool", "comment-density", "--json"], input=_MUST_DENY["ratio_what_narration"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed[0]["path"] == "src/teatree/x.py"
        assert parsed[0]["comment_lines"] >= 3

    def test_json_output_clean_is_empty_list(self) -> None:
        result = runner.invoke(app, ["tool", "comment-density", "--json"], input=_MUST_ALLOW["sparse_code"])
        assert result.exit_code == 0
        assert json.loads(result.output) == []

    def test_staged_source_reads_git_diff_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.cli import comment_density_tools  # noqa: PLC0415

        class _Result:
            stdout = _MUST_DENY["scattered_ratio"]

        monkeypatch.setattr(comment_density_tools, "run_allowed_to_fail", lambda *a, **k: _Result())
        result = runner.invoke(app, ["tool", "comment-density", "--staged"])
        assert result.exit_code == 0
        assert "comment-density warning" in result.output

    def test_base_ref_source_reads_three_dot_diff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.cli import comment_density_tools  # noqa: PLC0415

        captured: dict[str, object] = {}

        class _Result:
            stdout = _MUST_ALLOW["sparse_code"]

        def _fake(cmd: list[str], **_kwargs: object) -> _Result:
            captured["cmd"] = cmd
            return _Result()

        monkeypatch.setattr(comment_density_tools, "run_allowed_to_fail", _fake)
        result = runner.invoke(app, ["tool", "comment-density", "--base-ref", "origin/main"])
        assert result.exit_code == 0
        assert "origin/main...HEAD" in captured["cmd"]


class TestPrePushHook:
    """The standalone hook warns on a staged comment-dense diff but never blocks."""

    def _run_hook(self, repo: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )

    def _init_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run([GIT, "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run([GIT, "config", "user.email", "t@example.com"], cwd=repo, check=True)
        subprocess.run([GIT, "config", "user.name", "t"], cwd=repo, check=True)
        return repo

    def test_clean_staged_diff_exits_zero(self, tmp_path: Path) -> None:
        repo = self._init_repo(tmp_path)
        src = repo / "src" / "teatree"
        src.mkdir(parents=True)
        (src / "x.py").write_text("def handle(value):\n    return value + 1\n", encoding="utf-8")
        subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)
        result = self._run_hook(repo)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_comment_dense_staged_diff_warns_and_exits_zero(self, tmp_path: Path) -> None:
        repo = self._init_repo(tmp_path)
        src = repo / "src" / "teatree"
        src.mkdir(parents=True)
        (src / "x.py").write_text(
            "def handle(value):\n"
            "    # increment the counter by one\n"
            "    # then store it in the cache\n"
            "    # so later reads are fast\n"
            "    counter += 1\n"
            "    cache[key] = counter\n",
            encoding="utf-8",
        )
        subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)
        result = self._run_hook(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "comment-density warning" in result.stdout
        assert "advisory only" in result.stdout

    def test_empty_staged_diff_exits_zero(self, tmp_path: Path) -> None:
        repo = self._init_repo(tmp_path)
        result = self._run_hook(repo)
        assert result.returncode == 0, result.stdout + result.stderr
