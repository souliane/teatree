"""The extracted spoken-text cleaning concern, tested against the sibling module directly.

``clean_for_speech`` lives in ``teatree.core.speak_cleaning`` (re-exported from
``teatree.core.speak``). ``test_speak.py`` exercises it through the ``speak``
re-export; this mirror names the owning module's public symbol directly so the
per-diff coverage sees the seam that defines it.
"""

from teatree.core.speak_cleaning import _MAX_SPEAK_CHARS, clean_for_speech


class TestCleanForSpeech:
    def test_strips_code_and_urls_keeps_prose(self) -> None:
        out = clean_for_speech("see [the PR](https://example.com/pr/1): ```x = 1``` shipped")
        assert "the PR" in out
        assert "shipped" in out
        assert "x = 1" not in out
        assert "example.com" not in out

    def test_drops_status_and_log_noise_lines(self) -> None:
        out = clean_for_speech(":information_source: *info*\nINFO: warming\nThe deploy finished.")
        assert out == "The deploy finished."

    def test_keeps_level_word_without_discriminator_as_prose(self) -> None:
        assert clean_for_speech("Critical bug found in prod.") == "Critical bug found in prod."

    def test_caps_length_on_word_boundary(self) -> None:
        out = clean_for_speech("word " * 400)
        assert len(out) <= _MAX_SPEAK_CHARS + 1
        assert out.endswith("…")
