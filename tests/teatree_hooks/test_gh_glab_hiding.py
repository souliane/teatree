"""Quote-aware substitution primitives for the publish-surface carve-out (#1415).

``raw_has_live_substitution`` classifies a substitution marker as LIVE (bash
would expand it) only outside a single-quoted span, and ``command_segments_with_raw``
carries each WORD token's verbatim source span alongside its decoded value so the
classification has the quoting context the decoded value discards.
"""

import pytest

from teatree.hooks._gh_glab_hiding import command_segments, command_segments_with_raw, raw_has_live_substitution


class TestRawHasLiveSubstitution:
    @pytest.mark.parametrize(
        "raw",
        [
            "$(whoami)",  # bare command substitution
            '"cost $(echo x)"',  # inside double quotes -- still expanded
            "`date`",  # backtick command substitution
            "<(cat f)",  # process substitution
            '"it\'s $(cat s)"',  # apostrophe in a double-quoted span is literal; the $() is live
            "",  # empty raw span -> conservative LIVE
        ],
    )
    def test_live_markers(self, raw: str) -> None:
        assert raw_has_live_substitution(raw) is True

    @pytest.mark.parametrize(
        "raw",
        [
            "'refactor the `svc` module'",  # single-quoted backtick -- inert
            "'see $(svc) note'",  # single-quoted $() -- inert
            "'<(not a real sub)'",  # single-quoted process-sub marker -- inert
            "'plain body no markers'",  # no marker at all
            "--description",  # an ordinary flag token
        ],
    )
    def test_inert_or_absent_markers(self, raw: str) -> None:
        assert raw_has_live_substitution(raw) is False


class TestCommandSegmentsWithRaw:
    def test_words_and_raws_are_index_aligned(self) -> None:
        cmd = "glab mr create -R ns/repo --description 'the `svc` bit'"
        segments = command_segments_with_raw(cmd)
        assert len(segments) == 1
        words, raws = segments[0]
        assert len(words) == len(raws)
        # The body token: decoded value strips the single quotes; raw keeps them.
        assert words[-1] == "the `svc` bit"
        assert raws[-1] == "'the `svc` bit'"

    def test_decoded_view_matches_command_segments(self) -> None:
        cmd = 'cd /w && FOO=1 gh pr create -R ns/repo --body "x $(echo y)" ; echo done'
        assert [words for words, _ in command_segments_with_raw(cmd)] == command_segments(cmd)

    def test_leading_env_assignment_stripped(self) -> None:
        words, raws = command_segments_with_raw("FOO=1 glab mr create -R ns/repo")[0]
        assert words[0] == "glab"
        assert raws[0] == "glab"
