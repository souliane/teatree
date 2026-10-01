"""No shipped doc names a retired setting except to say it is retired.

A retired key still WRITES — ``ConfigSetting.set_value`` does not refuse it — so an
operator who follows a doc naming it as a live kill-switch flips a dead row and nothing
stops. BLUEPRINT and the README went on presenting ``loop_runner_enabled`` as the fleet's
emergency stop, and a dozen retired per-loop ``*_disabled`` scalars as live switches,
for weeks after the ledger retired them.

Naming a retired key to say it is retired is legitimate, so those sentences are
ENUMERATED in ``retired_setting_notice_sentences.txt``, verbatim — the precedent
``test_retired_egress_control_is_not_instructed`` set, for the reason it gives: a
keyword marker ("retired", "removed") reads naturally inside an instruction too.

Reach: backticked spellings in markdown, split into sentences. A plain-English mention,
or a Python docstring, is not scanned.
"""

import re
from pathlib import Path

from teatree.config.retired_settings_ledger import RETIRED_SETTINGS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROSE_ROOTS = ("docs", "skills", "agents", "hooks")
_NOTICE_PATH = Path(__file__).with_name("retired_setting_notice_sentences.txt")

# test_retired_egress_control_is_not_instructed owns these, with its own notice list.
_OWNED_ELSEWHERE = frozenset({"on_behalf_post_mode", "ask_before_post_on_behalf"})

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def retired_key_pattern() -> re.Pattern[str]:
    keys = sorted({entry.key for entry in RETIRED_SETTINGS} - _OWNED_ELSEWHERE)
    return re.compile("`(?:" + "|".join(map(re.escape, keys)) + ")`")


def scanned_docs() -> list[Path]:
    nested = [path for root in _PROSE_ROOTS for path in sorted((_REPO_ROOT / root).rglob("*.md"))]
    return sorted(_REPO_ROOT.glob("*.md")) + nested


def sentences_naming_a_retired_key(text: str, pattern: re.Pattern[str]) -> list[str]:
    return [
        sentence.strip()
        for line in text.splitlines()
        for sentence in _SENTENCE_END.split(line)
        if pattern.search(sentence)
    ]


def notice_sentences() -> frozenset[str]:
    lines = _NOTICE_PATH.read_text(encoding="utf-8").splitlines()
    return frozenset(line.strip() for line in lines if line.strip() and not line.startswith("#"))


def test_no_doc_names_a_retired_setting_outside_an_enumerated_notice() -> None:
    pattern, notices = retired_key_pattern(), notice_sentences()
    offenders = sorted(
        f"{path.relative_to(_REPO_ROOT)}: {sentence}"
        for path in scanned_docs()
        for sentence in sentences_naming_a_retired_key(path.read_text(encoding="utf-8"), pattern)
        if sentence not in notices
    )

    assert not offenders, (
        "a doc names a retired setting — describe what replaced it, or, if the sentence says it is "
        f"retired, add it verbatim to {_NOTICE_PATH.name}:\n" + "\n".join(offenders)
    )


def test_every_enumerated_notice_is_still_in_a_doc() -> None:
    pattern = retired_key_pattern()
    present = {
        sentence
        for path in scanned_docs()
        for sentence in sentences_naming_a_retired_key(path.read_text(encoding="utf-8"), pattern)
    }

    assert notice_sentences() <= present, (
        f"{_NOTICE_PATH.name} lists sentences no doc carries any more — drop them: "
        + "\n".join(sorted(notice_sentences() - present))
    )


class TestTheScanTellsALiveKnobFromALiveSetting:
    """The control: the pattern must catch a retired key and pass a live one."""

    def test_a_retired_key_named_as_a_switch_is_caught(self) -> None:
        text = "Daily cadence. `db_backup_disabled` kill-switch; mechanical-only."

        assert sentences_naming_a_retired_key(text, retired_key_pattern()) == [
            "`db_backup_disabled` kill-switch; mechanical-only."
        ]

    def test_a_live_setting_is_not_caught(self) -> None:
        text = "Retention is `db_backup_retention_days`."

        assert sentences_naming_a_retired_key(text, retired_key_pattern()) == []

    def test_the_loop_fleet_switch_is_in_the_scanned_set(self) -> None:
        assert retired_key_pattern().search("`loop_runner_enabled`")
