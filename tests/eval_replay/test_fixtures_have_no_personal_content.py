"""Privacy guard: committed eval fixtures must be SYNTHETIC, never a real transcript.

``evals/fixtures/*.jsonl`` are the synthetic transcripts the ``transcript`` backend
grades in CI (produced by the corpus-gen pipeline, ``fixt-`` session ids). This
repo is PUBLIC, so a REAL captured session transcript — which carries personal
content, identity strings, or off-color language from a live session — must NEVER
land here. The runtime capture target is gitignored (``/*.jsonl`` in ``.gitignore``)
so a real transcript cannot be ``git add``ed by accident; this test is the
belt-to-that-suspenders deterministic backstop: it scans every committed fixture
for personal markers and fails LOUD if one appears.

A hit is not a lint nit — it is a privacy incident. The fix is to remove the real
transcript and regenerate a synthetic one via the corpus-gen pipeline, never to
loosen this guard.
"""

import re
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "evals" / "fixtures"

#: Synthetic placeholder home-dir owners a fixture may legitimately use in an
#: EXAMPLE path (``/Users/x``, ``/Users/example`` …). A real transcript's home
#: path carries the operator's actual username instead — anything NOT on this
#: allowlist trips the local-home check below.
_PLACEHOLDER_HOME_OWNERS = frozenset({"x", "example", "user", "you", "me", "dev", "someone", "ci", "home"})

#: Personal / identity / off-color markers that betray a REAL session transcript.
#: Word-boundary regexes (case-insensitive) so a substring inside an innocuous
#: token does not false-fire. The synthetic corpus is deliberately neutral, so the
#: clean set passes; a real transcript trips at least one.
_PERSONAL_MARKER_PATTERNS: tuple[str, ...] = (
    # Identity strings — a real session names the operator / their address.
    r"\bjohn\.soulo\b",
    r"\bproton\.me\b",
    # Curse / off-color language — a clean synthetic corpus carries none.
    r"\bfuck\w*\b",
    r"\bshit\b",
    r"\basshole\b",
    r"\bbastard\b",
    r"\bbitch\b",
    # Credential shapes — a real transcript may echo a leaked secret. Tuned to the
    # REAL token structure (multi-segment, long random tails) so a synthetic
    # placeholder a fixture uses to TEST leak-detection (`xoxb-placeholder`) does
    # NOT trip, but an actually-leaked credential does.
    r"\bxox[baprs]-\d{8,}-\d{8,}-[A-Za-z0-9]{16,}",  # Slack token (segmented)
    r"\bglpat-[A-Za-z0-9_-]{20,}",  # GitLab PAT
    r"\bghp_[A-Za-z0-9]{36,}",  # GitHub PAT
    r"\bsk-ant-api03-[A-Za-z0-9_-]{20,}",  # Anthropic API key
)

_COMPILED = [re.compile(pattern, re.IGNORECASE) for pattern in _PERSONAL_MARKER_PATTERNS]

#: A real local home path: ``/Users/<owner>`` or ``/home/<owner>`` whose owner is
#: NOT a synthetic placeholder. Captured separately so the placeholder allowlist
#: applies without losing the real-home signal.
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/([A-Za-z0-9_.-]+)")


def _real_home_owners(text: str) -> set[str]:
    return {owner for owner in _HOME_PATH_RE.findall(text) if owner.lower() not in _PLACEHOLDER_HOME_OWNERS}


def _fixture_files() -> list[Path]:
    return sorted(FIXTURES_DIR.glob("*.jsonl"))


def test_fixtures_dir_exists_and_is_non_empty() -> None:
    assert FIXTURES_DIR.is_dir(), f"missing fixtures dir: {FIXTURES_DIR}"
    assert _fixture_files(), "no fixtures found — the privacy scan would be vacuous"


@pytest.mark.parametrize("fixture", _fixture_files(), ids=lambda p: p.name)
def test_fixture_has_no_personal_marker(fixture: Path) -> None:
    text = fixture.read_text(encoding="utf-8", errors="replace")
    hits = sorted({pattern.pattern for pattern in _COMPILED if pattern.search(text)})
    real_homes = _real_home_owners(text)
    if real_homes:
        hits.append(f"real home path owner(s): {sorted(real_homes)}")
    assert not hits, (
        f"{fixture.name} carries personal/identity/credential markers {hits} — a REAL "
        "captured transcript must NEVER be committed to this PUBLIC repo. Remove it and "
        "regenerate a synthetic fixture via the corpus-gen pipeline."
    )


def test_guard_catches_a_planted_identity_marker() -> None:
    # Anti-vacuity: prove the scan would FAIL on a real-transcript marker, so a
    # green run above means the fixtures are clean, not that the scan is a no-op.
    planted = "the operator is john.soulo@proton.me"
    hits = [pattern.pattern for pattern in _COMPILED if pattern.search(planted)]
    assert hits, "the personal-marker scan failed to catch a planted identity string"


def test_guard_catches_a_real_credential_but_allows_a_placeholder() -> None:
    # A real-shaped Slack bot token trips; the synthetic `xoxb-placeholder` a
    # leak-detection fixture uses does NOT. The real-shaped sample is ASSEMBLED at
    # runtime (segments + a generated tail) so no high-entropy literal lands in the
    # file — the secret-scanner would otherwise flag this anti-vacuity assertion.
    real_shaped = "-".join(["xoxb", "1" * 8, "2" * 8, "A1b2C3d4" * 2])
    assert any(pattern.search(real_shaped) for pattern in _COMPILED)
    assert not any(pattern.search("xoxb-placeholder") for pattern in _COMPILED)


def test_guard_catches_a_real_home_path_but_allows_placeholders() -> None:
    # A real username trips; the synthetic placeholders (/Users/x, /Users/example)
    # the fixtures legitimately use do NOT.
    assert _real_home_owners("ran in /Users/realname/workspace") == {"realname"}
    assert _real_home_owners("ran in /Users/x and /Users/example and /home/user") == set()
