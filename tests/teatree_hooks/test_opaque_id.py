"""Opaque Slack/forge ID leak detector tests.

No gate caught Slack/forge IDs before: a channel/DM/user/app/team id such
as a real ``C0...``/``D0...``/``U0...``/``A0...``/``T0...`` token is an
internal identifier that must never reach a public surface. This detector
flags the real shape while a synthetic-placeholder ALLOWLIST keeps test
fixtures and documentation examples (``C0DEMO*``, ``U01ABCD1234``,
``C_REVIEW`` …) from tripping it.

All IDs here are SYNTHETIC. The ``real-shaped`` cases use random-looking
but invented tokens; no real channel/DM/user/app id appears.
"""

import pytest

from teatree.hooks.opaque_id import find_opaque_ids, is_synthetic_placeholder

# Real-SHAPED (random, non-synthetic) opaque ids — these MUST be flagged.
# Invented here; not a real channel/DM/user/app/team id.
_REAL_SHAPED: tuple[str, ...] = (
    "C0ZX91QWERT",  # channel
    "D0KP47MNBVC",  # DM
    "U0RT83HGFDS",  # user
    "A0YU52POIUL",  # app id
    "T0WE61LKJHG",  # team id
)

# Synthetic placeholders — these MUST NOT be flagged.
_SYNTHETIC: tuple[str, ...] = (
    "C0DEMOCHAN1",
    "D0DEMOCLNT1",
    "D0DEMOTEAM1",
    "U0DEMOUSER1",
    "A0DEMOAPP01",
    "T0DEMOTEAM1",
    "D0CACHED",
    "U01ABCD1234",
    "A01ABCD1234",
    "C01ABCD1234",
    "C_REVIEW",
    "U0AAAAAAAAA",  # one repeated char
    "D0000000001",  # all zeros + 1
    "C0COLLEAGUE1",
    "U0GLOBALUSER",
    "D0BUNKNOWN99",
    "C09INTERNAL0",
)


class TestIsSyntheticPlaceholder:
    @pytest.mark.parametrize("token", _SYNTHETIC)
    def test_synthetic_forms_are_allowlisted(self, token: str) -> None:
        assert is_synthetic_placeholder(token) is True

    @pytest.mark.parametrize("token", _REAL_SHAPED)
    def test_real_shaped_forms_are_not_allowlisted(self, token: str) -> None:
        assert is_synthetic_placeholder(token) is False


class TestFindOpaqueIds:
    @pytest.mark.parametrize("token", _REAL_SHAPED)
    def test_real_shaped_id_is_found(self, token: str) -> None:
        hits = find_opaque_ids(f"channel = {token!r}")
        assert hits == [token]

    @pytest.mark.parametrize("token", _SYNTHETIC)
    def test_synthetic_placeholder_is_not_found(self, token: str) -> None:
        assert find_opaque_ids(f"channel = {token!r}") == []

    def test_id_inside_a_slack_archive_url_is_found(self) -> None:
        hits = find_opaque_ids("https://slack.com/archives/C0ZX91QWERT/p1717603200123456")
        assert "C0ZX91QWERT" in hits

    def test_id_glued_to_a_colon_thread_ts_is_found(self) -> None:
        hits = find_opaque_ids("posted slack:C0ZX91QWERT:1717603200.123456")
        assert "C0ZX91QWERT" in hits

    def test_clean_text_has_no_hits(self) -> None:
        assert find_opaque_ids("a normal sentence with no identifiers at all") == []

    def test_substring_inside_a_longer_token_is_not_a_hit(self) -> None:
        # The shape needs a token boundary — a longer alnum run is not an id.
        assert find_opaque_ids("XC0ZX91QWERTX") == []

    def test_multiple_ids_on_one_line_all_found(self) -> None:
        hits = find_opaque_ids("from C0ZX91QWERT to D0KP47MNBVC")
        assert hits == ["C0ZX91QWERT", "D0KP47MNBVC"]

    def test_synthetic_and_real_on_same_line_only_real_found(self) -> None:
        hits = find_opaque_ids("demo C0DEMOCHAN1 real C0ZX91QWERT")
        assert hits == ["C0ZX91QWERT"]

    def test_lowercase_is_not_matched(self) -> None:
        # Opaque ids are uppercase; a lowercase run is not an id.
        assert find_opaque_ids("c0zx91qwert") == []

    def test_line_with_allow_marker_is_exempt(self) -> None:
        # A line carrying the inline allow-marker is exempt — used by the
        # detector's own fixtures and any legitimate one-off example.
        assert find_opaque_ids("channel C0ZX91QWERT  # leak-scan:allow") == []
