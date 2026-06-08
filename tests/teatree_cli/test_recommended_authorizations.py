"""Tests for the recommended auto-mode authorization detection.

Integration-first per the Test-Writing Doctrine: a **real** temp
``settings.json`` fixture replaces patching the JSON loader. The only
boundary mocked is ``Path.home()`` so ``~/.claude/settings.json`` lookups
are sandboxed away from the real user file (an unstoppable external — we
must never read or write the developer's real ``~/.claude``).

The doctor-check assertions also prove the file is never modified
(read-only detection) and that absence degrades gracefully.
"""

import json
from pathlib import Path

from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.recommended_authorizations import (
    RECOMMENDED_AUTHORIZATIONS,
    RecommendedAuthorization,
    find_missing_authorizations,
    load_automode_allow,
    report_missing_authorizations,
)

runner = CliRunner()


def _write_settings(home: Path, payload: object) -> Path:
    """Create ``<home>/.claude/settings.json`` with ``payload`` as JSON."""
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(payload), encoding="utf-8")
    return settings


def _stage_home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect ``Path.home()`` into ``tmp_path`` (sandbox the user file)."""
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _all_recommended_sentences() -> list[str]:
    return [rec.sentence for rec in RECOMMENDED_AUTHORIZATIONS]


# ── load_automode_allow ──────────────────────────────────────────────────


class TestLoadAutomodeAllow:
    def test_returns_empty_when_file_absent(self, tmp_path):
        assert load_automode_allow(tmp_path / "nope.json") == []

    def test_returns_empty_when_not_json(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{ this is not json", encoding="utf-8")
        assert load_automode_allow(path) == []

    def test_returns_empty_when_top_level_not_object(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert load_automode_allow(path) == []

    def test_returns_empty_when_automode_missing(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"permissions": {"allow": ["x"]}}), encoding="utf-8")
        assert load_automode_allow(path) == []

    def test_returns_empty_when_automode_not_object(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"autoMode": "weird"}), encoding="utf-8")
        assert load_automode_allow(path) == []

    def test_returns_empty_when_allow_not_list(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"autoMode": {"allow": "nope"}}), encoding="utf-8")
        assert load_automode_allow(path) == []

    def test_filters_non_string_entries(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"autoMode": {"allow": ["keep me", 42, None, {"x": 1}]}}),
            encoding="utf-8",
        )
        assert load_automode_allow(path) == ["keep me"]

    def test_reads_resolved_home_settings_when_no_arg(self, tmp_path, monkeypatch):
        home = _stage_home(tmp_path, monkeypatch)
        _write_settings(home, {"autoMode": {"allow": ["hello world"]}})
        assert load_automode_allow() == ["hello world"]

    def test_follows_symlinked_settings_file(self, tmp_path, monkeypatch):
        home = _stage_home(tmp_path, monkeypatch)
        real = tmp_path / "dotfiles" / "claude-settings.json"
        real.parent.mkdir(parents=True)
        real.write_text(json.dumps({"autoMode": {"allow": ["via symlink"]}}), encoding="utf-8")
        link_dir = home / ".claude"
        link_dir.mkdir(parents=True)
        (link_dir / "settings.json").symlink_to(real)
        assert load_automode_allow() == ["via symlink"]

    def test_returns_empty_when_home_settings_absent(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        assert load_automode_allow() == []


# ── RecommendedAuthorization.is_covered_by ───────────────────────────────


class TestIsCoveredBy:
    rec = RecommendedAuthorization(
        key="x",
        sentence="...",
        keyphrases=("alpha", "beta"),
    )

    def test_uncovered_when_no_entries(self):
        assert self.rec.is_covered_by([]) is False

    def test_uncovered_when_only_some_keyphrases_present(self):
        assert self.rec.is_covered_by(["this has Alpha only"]) is False

    def test_covered_when_all_keyphrases_present_case_insensitive(self):
        assert self.rec.is_covered_by(["ALPHA and BETA appear here"]) is True

    def test_covered_by_any_one_entry(self):
        assert self.rec.is_covered_by(["unrelated", "alpha beta together"]) is True


# ── find_missing_authorizations ──────────────────────────────────────────


class TestFindMissingAuthorizations:
    def test_all_missing_when_no_settings(self, tmp_path):
        missing = find_missing_authorizations(tmp_path / "absent.json")
        assert missing == list(RECOMMENDED_AUTHORIZATIONS)

    def test_pasting_recommended_sentences_clears_everything(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"autoMode": {"allow": _all_recommended_sentences()}}),
            encoding="utf-8",
        )
        assert find_missing_authorizations(path) == []

    def test_detects_a_single_missing_rule(self, tmp_path):
        # Cover all but the sanctioned-merge-path rule.
        kept = [r for r in RECOMMENDED_AUTHORIZATIONS if r.key != "sanctioned-merge-path"]
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"autoMode": {"allow": [r.sentence for r in kept]}}),
            encoding="utf-8",
        )
        missing = find_missing_authorizations(path)
        assert [r.key for r in missing] == ["sanctioned-merge-path"]

    def test_loosely_worded_rule_still_counts_as_covered(self, tmp_path):
        # A user's own paraphrase that still contains both keyphrases counts.
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "autoMode": {
                        "allow": [
                            "Let the agent run `gh pr merge` when checks are green and clean.",
                        ],
                    },
                },
            ),
            encoding="utf-8",
        )
        missing_keys = {r.key for r in find_missing_authorizations(path)}
        assert "gh-pr-merge-green-only" not in missing_keys

    def test_does_not_modify_the_settings_file(self, tmp_path):
        path = tmp_path / "settings.json"
        original = json.dumps({"autoMode": {"allow": ["something unrelated"]}})
        path.write_text(original, encoding="utf-8")
        find_missing_authorizations(path)
        assert path.read_text(encoding="utf-8") == original


class TestRecommendedSetIntegrity:
    def test_keys_are_unique(self):
        keys = [r.key for r in RECOMMENDED_AUTHORIZATIONS]
        assert len(keys) == len(set(keys))

    def test_every_rec_has_keyphrases_and_sentence(self):
        for rec in RECOMMENDED_AUTHORIZATIONS:
            assert rec.sentence.strip()
            assert rec.keyphrases
            assert all(p == p.lower() for p in rec.keyphrases)

    def test_no_user_specific_tokens_leak_into_generic_set(self):
        # The recommended set must stay teatree-generic — structural check
        # rather than a customer-name denylist (which would itself be a
        # banned-terms violation in this public repo). User-specific shapes:
        # absolute home paths, email addresses, and bare URLs.
        for rec in RECOMMENDED_AUTHORIZATIONS:
            text = rec.sentence
            assert "/Users/" not in text, rec.key
            assert "/home/" not in text, rec.key
            assert "@" not in text, rec.key
            assert "https://" not in text, rec.key
            assert "http://" not in text, rec.key
            # The only literal `~/` paths allowed are the generic Claude
            # config dirs the manage-settings rule legitimately names.
            if rec.key != "manage-claude-settings-and-hooks":
                assert "~/" not in text, rec.key


# ── t3 doctor CLI surface ────────────────────────────────────────────────


class TestDoctorAuthorizationsCommand:
    def test_warns_and_prints_paste_ready_sentence_for_missing(self, tmp_path, monkeypatch):
        home = _stage_home(tmp_path, monkeypatch)
        # Cover everything except the sanctioned merge path.
        kept = [r for r in RECOMMENDED_AUTHORIZATIONS if r.key != "sanctioned-merge-path"]
        _write_settings(home, {"autoMode": {"allow": [r.sentence for r in kept]}})

        result = runner.invoke(app, ["doctor", "authorizations"])

        assert result.exit_code == 0
        assert "WARN" in result.output
        merge_rec = next(r for r in RECOMMENDED_AUTHORIZATIONS if r.key == "sanctioned-merge-path")
        assert merge_rec.sentence in result.output
        # Other (covered) rules must not be re-suggested.
        assert kept[0].sentence not in result.output

    def test_reports_all_present_when_fully_covered(self, tmp_path, monkeypatch):
        home = _stage_home(tmp_path, monkeypatch)
        _write_settings(home, {"autoMode": {"allow": _all_recommended_sentences()}})

        result = runner.invoke(app, ["doctor", "authorizations"])

        assert result.exit_code == 0
        assert "OK" in result.output
        assert "WARN" not in result.output

    def test_degrades_gracefully_when_settings_absent(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)  # no settings.json written

        result = runner.invoke(app, ["doctor", "authorizations"])

        assert result.exit_code == 0
        # Every recommendation is reported missing, with guidance.
        assert "autoMode.allow" in result.output
        for rec in RECOMMENDED_AUTHORIZATIONS:
            assert rec.sentence in result.output

    def test_does_not_write_user_settings_file(self, tmp_path, monkeypatch):
        home = _stage_home(tmp_path, monkeypatch)
        settings = _write_settings(home, {"autoMode": {"allow": []}})
        before = settings.read_text(encoding="utf-8")

        runner.invoke(app, ["doctor", "authorizations"])

        assert settings.read_text(encoding="utf-8") == before

    def test_report_is_advisory_and_injects_echo(self, tmp_path):
        # The session-start path runs `t3 doctor check`, which calls
        # report_missing_authorizations(typer.echo). It must never fail the
        # overall check and must route all output through the injected echo.
        printed: list[str] = []

        result = report_missing_authorizations(
            lambda msg="": printed.append(str(msg)),
            tmp_path / "absent.json",
        )

        blob = "\n".join(printed)
        assert result is True  # advisory, never blocks the check
        assert "WARN" in blob
        assert "autoMode.allow" in blob

    def test_report_says_ok_when_fully_covered(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"autoMode": {"allow": _all_recommended_sentences()}}),
            encoding="utf-8",
        )
        printed: list[str] = []

        result = report_missing_authorizations(lambda msg="": printed.append(str(msg)), path)

        assert result is True
        assert any("OK" in line for line in printed)
        assert all("WARN" not in line for line in printed)
