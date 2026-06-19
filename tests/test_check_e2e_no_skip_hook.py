"""Tests for the deterministic E2E no-skip gate (souliane/teatree#1967 family).

The hook scans staged E2E spec files (``e2e/**/*.spec.ts`` plus any overlay
e2e dir) for skip/quarantine markers — ``test.skip(`` / ``test.only(`` /
``test.fixme(`` / ``it.skip(`` / ``xit(`` / ``describe.skip(`` — and for
``// TODO`` / ``// FIXME`` comments inside spec bodies, and fails closed with
``file:line`` for each finding.

A green spec (no markers, no body-TODO) is silent. A spec carrying a marker
fails the gate; the agent must remove or replace it before the push lands.
"""

import pytest

from scripts.hooks.check_e2e_no_skip import Finding, is_spec_path, scan_spec_lines, scan_specs


def _lines(text: str) -> list[str]:
    return text.splitlines()


class TestIsSpecPath:
    def test_top_level_e2e_spec_matches(self) -> None:
        assert is_spec_path("e2e/login.spec.ts")

    def test_nested_e2e_spec_matches(self) -> None:
        assert is_spec_path("e2e/flows/checkout/cart.spec.ts")

    def test_overlay_e2e_spec_matches(self) -> None:
        assert is_spec_path("some-overlay/e2e/smoke.spec.ts")
        assert is_spec_path("packages/widget/e2e/nested/a.spec.ts")

    def test_non_spec_file_does_not_match(self) -> None:
        assert not is_spec_path("e2e/helpers.ts")
        assert not is_spec_path("e2e/README.md")

    def test_spec_outside_e2e_dir_does_not_match(self) -> None:
        # A *.spec.ts elsewhere (a frontend unit spec) is out of scope.
        assert not is_spec_path("src/app/login.spec.ts")
        assert not is_spec_path("frontend/cart.spec.ts")


class TestScanSpecLines:
    @pytest.mark.parametrize(
        "marker",
        [
            "  test.skip('does the thing', async () => {",
            "  test.only('isolates this', async () => {",
            "  test.fixme('broken for now', async () => {",
            "  it.skip('legacy mocha style', () => {",
            "  xit('disabled mocha test', () => {",
            "  describe.skip('whole block off', () => {",
        ],
    )
    def test_skip_marker_is_flagged(self, marker: str) -> None:
        findings = scan_spec_lines("e2e/a.spec.ts", _lines(marker))
        assert len(findings) == 1
        assert findings[0].path == "e2e/a.spec.ts"
        assert findings[0].line == 1

    def test_line_number_is_one_based_and_correct(self) -> None:
        body = "import { test } from '@playwright/test';\n\ntest.skip('x', () => {});\n"
        findings = scan_spec_lines("e2e/a.spec.ts", _lines(body))
        assert len(findings) == 1
        assert findings[0].line == 3

    @pytest.mark.parametrize(
        "comment",
        [
            "  // TODO: finish asserting the redirect",
            "  // FIXME: flaky on CI, needs a retry",
            "  await page.click('#go'); // TODO real selector",
        ],
    )
    def test_body_todo_fixme_comment_is_flagged(self, comment: str) -> None:
        findings = scan_spec_lines("e2e/a.spec.ts", _lines(comment))
        assert len(findings) == 1

    def test_clean_spec_yields_no_findings(self) -> None:
        body = (
            "import { test, expect } from '@playwright/test';\n"
            "test('logs in', async ({ page }) => {\n"
            "  await page.goto('/login');\n"
            "  await expect(page).toHaveTitle(/Home/);\n"
            "});\n"
        )
        assert scan_spec_lines("e2e/a.spec.ts", _lines(body)) == []

    def test_skip_substring_in_identifier_is_not_flagged(self) -> None:
        # `skipLink` / `describeForm` must not trip the marker regex.
        body = "const skipLink = page.locator('#skip');\ntest('uses describeForm', () => {});\n"
        assert scan_spec_lines("e2e/a.spec.ts", _lines(body)) == []

    def test_todo_inside_string_literal_is_not_flagged(self) -> None:
        # A user-facing string "TODO list" is not a code comment.
        body = "test('renders TODO list page', async ({ page }) => {\n  await page.goto('/todos');\n});\n"
        assert scan_spec_lines("e2e/a.spec.ts", _lines(body)) == []

    def test_multiple_findings_reported_with_each_line(self) -> None:
        body = "test.skip('a', () => {});\n// TODO later\ntest.only('b', () => {});\n"
        findings = scan_spec_lines("e2e/a.spec.ts", _lines(body))
        assert [f.line for f in findings] == [1, 2, 3]


class TestScanSpecs:
    def test_scans_only_spec_paths(self, tmp_path) -> None:
        spec = tmp_path / "e2e" / "a.spec.ts"
        spec.parent.mkdir(parents=True)
        spec.write_text("test.skip('x', () => {});\n", encoding="utf-8")
        helper = tmp_path / "e2e" / "helpers.ts"
        helper.write_text("test.skip('not a spec', () => {});\n", encoding="utf-8")

        findings = scan_specs(["e2e/a.spec.ts", "e2e/helpers.ts"], root=tmp_path)
        assert len(findings) == 1
        assert findings[0].path == "e2e/a.spec.ts"

    def test_missing_file_is_skipped(self, tmp_path) -> None:
        # A staged-then-deleted path must not crash the gate.
        assert scan_specs(["e2e/gone.spec.ts"], root=tmp_path) == []


class TestFindingMessage:
    def test_message_includes_file_line_and_marker(self) -> None:
        f = Finding(path="e2e/a.spec.ts", line=7, marker="test.skip(")
        msg = f.message
        assert "e2e/a.spec.ts:7" in msg
        assert "test.skip(" in msg


class TestMain:
    @staticmethod
    def _git(repo, *args: str) -> None:
        import subprocess  # noqa: PLC0415

        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)  # noqa: S607

    def _init_repo(self, tmp_path):
        self._git(tmp_path, "init", "-q", "-b", "main")
        self._git(tmp_path, "config", "user.email", "t@example.com")
        self._git(tmp_path, "config", "user.name", "t")
        return tmp_path

    def test_main_blocks_a_staged_skip_spec(self, tmp_path, monkeypatch, capsys) -> None:
        repo = self._init_repo(tmp_path)
        spec = repo / "e2e" / "login.spec.ts"
        spec.parent.mkdir(parents=True)
        spec.write_text("test.skip('off', () => {});\n", encoding="utf-8")
        self._git(repo, "add", "e2e/login.spec.ts")
        monkeypatch.chdir(repo)

        import scripts.hooks.check_e2e_no_skip as mod  # noqa: PLC0415

        assert mod.main() == 1
        out = capsys.readouterr().out
        assert "e2e/login.spec.ts:1" in out

    def test_main_silent_on_clean_staged_spec(self, tmp_path, monkeypatch, capsys) -> None:
        repo = self._init_repo(tmp_path)
        spec = repo / "e2e" / "login.spec.ts"
        spec.parent.mkdir(parents=True)
        spec.write_text("test('logs in', async ({ page }) => { await page.goto('/'); });\n", encoding="utf-8")
        self._git(repo, "add", "e2e/login.spec.ts")
        monkeypatch.chdir(repo)

        import scripts.hooks.check_e2e_no_skip as mod  # noqa: PLC0415

        assert mod.main() == 0
        assert capsys.readouterr().out == ""

    def test_main_silent_when_no_spec_staged(self, tmp_path, monkeypatch) -> None:
        repo = self._init_repo(tmp_path)
        (repo / "README.md").write_text("docs\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        monkeypatch.chdir(repo)

        import scripts.hooks.check_e2e_no_skip as mod  # noqa: PLC0415

        assert mod.main() == 0

    def test_main_propagates_git_failure_instead_of_exiting_zero(self, monkeypatch) -> None:
        import subprocess  # noqa: PLC0415

        import scripts.hooks.check_e2e_no_skip as mod  # noqa: PLC0415

        def _fail_run(*_args, **_kwargs):
            return subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="fatal")

        monkeypatch.setattr(subprocess, "run", _fail_run)
        with pytest.raises(subprocess.CalledProcessError):
            mod.main()
