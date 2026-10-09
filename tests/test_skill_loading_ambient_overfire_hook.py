"""Harness ambient context is not the owner's words (#1567).

The Claude Code harness appends ``<system-reminder>…</system-reminder>`` blocks (the
CLAUDE.md body, the MEMORY.md index, the available-skills listing) to the owner's
transcript entries. Those blocks carry many topic keywords that are NOT anything the
owner said — e.g. a MEMORY.md index line naming ``feedback_blog_*`` contains the word
``blog`` — so :func:`strip_ambient_context` drops them before a gate reads the owner's
messages.
"""

from hooks.scripts.skill_loader_input import _AMBIENT_STRIP_MAX_CHARS, strip_ambient_context

# The exact ambient shape that over-fired a keyword gate in #1567.
_MEMORY_INDEX_AMBIENT = (
    "<system-reminder>\n"
    "# claudeMd\n"
    "Codebase instructions below.\n"
    "- [feedback_blog_no_invented_confession_arcs.md] — Blog drafts: observational stance only\n"
    "Available skills: ac-writing-blog-posts: Write blog articles ...\n"
    "</system-reminder>"
)


class TestStripAmbientContext:
    """The pure stripper drops harness wrappers, keeps real task text."""

    def test_drops_system_reminder_block(self) -> None:
        stripped = strip_ambient_context(f"fix the parser bug\n{_MEMORY_INDEX_AMBIENT}")
        assert "blog" not in stripped.lower()
        assert "fix the parser bug" in stripped

    def test_keeps_real_intent_text(self) -> None:
        stripped = strip_ambient_context(f"write a blog post about teatree\n{_MEMORY_INDEX_AMBIENT}")
        assert "write a blog post about teatree" in stripped

    def test_drops_unterminated_block(self) -> None:
        # A truncated injection (no closing tag) must not leak ambient text.
        stripped = strip_ambient_context("do the refactor <system-reminder>\nblog blog blog")
        assert "blog" not in stripped.lower()
        assert "do the refactor" in stripped

    def test_drops_command_wrappers(self) -> None:
        stripped = strip_ambient_context("real task <command-name>blog</command-name>")
        assert "blog" not in stripped.lower()
        assert "real task" in stripped


class TestInputLengthCap:
    """The strip input is capped so the DOTALL regexes stay off the slow path.

    The block regex is O(n²) against many unterminated ``<system-reminder>``
    open tags (a pasted log/transcript, or a malicious agent). Since the
    strip runs on every owner message a gate reads, the input is capped to
    :data:`_AMBIENT_STRIP_MAX_CHARS` BEFORE matching. The deterministic
    assertion is that text beyond the cap is never seen by the matcher.
    """

    def test_text_beyond_cap_is_not_processed(self) -> None:
        # A sentinel keyword placed strictly beyond the cap must never reach
        # the output — proving the function processes only the capped slice
        # (deterministic, not timing-dependent).
        sentinel = "ZZSENTINELZZ"
        filler = "x" * (_AMBIENT_STRIP_MAX_CHARS + 100)
        stripped = strip_ambient_context(f"real task {filler}{sentinel}")
        assert sentinel not in stripped

    def test_text_within_cap_survives(self) -> None:
        # A keyword just inside the cap is still processed normally.
        sentinel = "ZZSENTINELZZ"
        prefix = "y" * (_AMBIENT_STRIP_MAX_CHARS - len(sentinel) - 10)
        stripped = strip_ambient_context(f"{prefix}{sentinel}")
        assert sentinel in stripped

    def test_unterminated_open_tag_flood_stays_fast(self) -> None:
        # Defense-in-depth (NOT the primary assertion): ~200 KB of unclosed
        # open tags — the O(n²) trigger — burns little CPU once the cap applies.
        # process_time (CPU, not wall-clock) keeps the guard immune to the
        # scheduler contention of a parallel `-n auto` run.
        import time  # noqa: PLC0415

        flood = "<system-reminder> blog " * 9000
        assert len(flood) > 200_000
        start = time.process_time()
        strip_ambient_context(flood)
        assert time.process_time() - start < 2.0
