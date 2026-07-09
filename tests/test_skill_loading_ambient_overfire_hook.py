r"""The skill suggester must ignore ambient harness context (#1567).

The Claude Code harness appends ``<system-reminder>…</system-reminder>``
blocks (the CLAUDE.md body, the MEMORY.md index, the available-skills
listing) to the prompt that reaches ``UserPromptSubmit``. Those blocks
carry many topic keywords that are NOT task intent — e.g. a MEMORY.md
index line naming ``feedback_blog_no_invented_confession_arcs.md``
contains the word ``blog``.

Pre-fix, the supplementary keyword matcher (``$HOME/.teatree-skills.yml``
mapping ``\\bblog\\b`` → ``ac-writing-blog-posts``) matched that ambient
line, wrote ``ac-writing-blog-posts`` into ``<session>.pending``, and the
PreToolUse gate then hard-blocked EVERY Bash/Edit/Write for the rest of
an autonomous loop doing unrelated work — a deadlock from a false
trigger.

The fix strips the harness ambient-context wrappers from the matcher
input at the source (:func:`hook_router._build_skill_loader_input`), so
the hard-block demand set derives only from genuine task-intent text. A
real ``blog`` keyword in the prompt body still suggests the skill.

Integration-style: the real ``_build_skill_loader_input`` +
``_strip_ambient_context``, the real ``suggest_skills`` engine, a real
trigger index built from fixture ``SKILL.md`` files, and a real
``$HOME/.teatree-skills.yml``-shaped supplementary config on disk.
"""

from __future__ import annotations  # noqa: TID251

import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _AMBIENT_STRIP_MAX_CHARS, _build_skill_loader_input, _strip_ambient_context

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import skill_loader as skill_loader_mod  # noqa: E402
from lib.skill_loader import suggest_skills  # noqa: E402

_BLOG_CONFIG = "ac-writing-blog-posts: '\\b(blog|article|write.?post|blog.?post)\\b'\n"

# A realistic harness-injected ambient block: the MEMORY.md index line
# that names a blog-feedback topic file. This is the exact shape that
# over-fired the gate in #1567.
_MEMORY_INDEX_AMBIENT = (
    "<system-reminder>\n"
    "# claudeMd\n"
    "Codebase instructions below.\n"
    "- [feedback_blog_no_invented_confession_arcs.md] — Blog drafts: observational stance only\n"
    "Available skills: ac-writing-blog-posts: Write blog articles ...\n"
    "</system-reminder>"
)


@pytest.fixture
def fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Seed a fixture skills tree, a supplementary config, and an empty cache.

    Returns ``(skills_dir, config)``. The XDG metadata cache is pointed at a
    missing path so ``suggest_skills`` builds the trigger index from the
    fixture skills dir (deterministic, host-independent).
    """
    skills_dir = tmp_path / "skills"
    (skills_dir / "ac-writing-blog-posts").mkdir(parents=True, exist_ok=True)
    (skills_dir / "ac-writing-blog-posts" / "SKILL.md").write_text(
        "---\nname: ac-writing-blog-posts\n---\n", encoding="utf-8"
    )
    (skills_dir / "code").mkdir(parents=True, exist_ok=True)
    (skills_dir / "code" / "SKILL.md").write_text(
        "---\nname: code\ntriggers:\n  priority: 70\n  keywords:\n    - '\\bimplement\\b'\n---\n",
        encoding="utf-8",
    )

    config = tmp_path / ".teatree-skills.yml"
    config.write_text(_BLOG_CONFIG, encoding="utf-8")

    monkeypatch.setattr(skill_loader_mod, "SKILL_METADATA_CACHE", tmp_path / "no-cache.json")
    monkeypatch.setattr(
        skill_loader_mod, "read_overlay_skill_metadata", lambda: {"skill_path": "", "remote_patterns": []}
    )
    monkeypatch.setattr(skill_loader_mod, "read_overlay_companion_skills", list)

    return skills_dir, config


def _suggest(prompt: str, skills_dir: Path, config: Path) -> list[str]:
    """Run the real matcher on *prompt* exactly as the hook does (ambient stripped)."""
    return suggest_skills(
        {
            "prompt": _strip_ambient_context(prompt),
            "cwd": str(skills_dir.parent),
            "loaded_skills": [],
            "skill_search_dirs": [str(skills_dir)],
            "supplementary_config": str(config),
        }
    )["suggestions"]


class TestStripAmbientContext:
    """The pure stripper drops harness wrappers, keeps real task text."""

    def test_drops_system_reminder_block(self) -> None:
        stripped = _strip_ambient_context(f"fix the parser bug\n{_MEMORY_INDEX_AMBIENT}")
        assert "blog" not in stripped.lower()
        assert "fix the parser bug" in stripped

    def test_keeps_real_intent_text(self) -> None:
        stripped = _strip_ambient_context(f"write a blog post about teatree\n{_MEMORY_INDEX_AMBIENT}")
        assert "write a blog post about teatree" in stripped

    def test_drops_unterminated_block(self) -> None:
        # A truncated injection (no closing tag) must not leak ambient text.
        stripped = _strip_ambient_context("do the refactor <system-reminder>\nblog blog blog")
        assert "blog" not in stripped.lower()
        assert "do the refactor" in stripped

    def test_drops_command_wrappers(self) -> None:
        stripped = _strip_ambient_context("real task <command-name>blog</command-name>")
        assert "blog" not in stripped.lower()
        assert "real task" in stripped

    def test_build_loader_input_strips_prompt(self, tmp_path: Path) -> None:
        # The hook's input builder must hand the matcher the stripped prompt.
        router_state = router.STATE_DIR
        router.STATE_DIR = tmp_path / "state"
        router.STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            built = _build_skill_loader_input(f"implement the fix\n{_MEMORY_INDEX_AMBIENT}", "sess-x")
        finally:
            router.STATE_DIR = router_state
        assert "blog" not in built["prompt"].lower()
        assert "implement the fix" in built["prompt"]


class TestInputLengthCap:
    """The strip input is capped so the DOTALL regexes stay off the slow path.

    The block regex is O(n²) against many unterminated ``<system-reminder>``
    open tags (a pasted log/transcript, or a malicious agent). Since the
    strip runs on every ``UserPromptSubmit``, the input is capped to
    :data:`_AMBIENT_STRIP_MAX_CHARS` BEFORE matching. The deterministic
    assertion is that text beyond the cap is never seen by the matcher.
    """

    def test_text_beyond_cap_is_not_processed(self) -> None:
        # A sentinel keyword placed strictly beyond the cap must never reach
        # the output — proving the function processes only the capped slice
        # (deterministic, not timing-dependent).
        sentinel = "ZZSENTINELZZ"
        filler = "x" * (_AMBIENT_STRIP_MAX_CHARS + 100)
        stripped = _strip_ambient_context(f"real task {filler}{sentinel}")
        assert sentinel not in stripped

    def test_text_within_cap_survives(self) -> None:
        # A keyword just inside the cap is still processed normally.
        sentinel = "ZZSENTINELZZ"
        prefix = "y" * (_AMBIENT_STRIP_MAX_CHARS - len(sentinel) - 10)
        stripped = _strip_ambient_context(f"{prefix}{sentinel}")
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
        _strip_ambient_context(flood)
        assert time.process_time() - start < 2.0


class TestAmbientKeywordDoesNotEnterSuggestions:
    """An ambient-only ``blog`` mention must not add a blog skill demand."""

    def test_ambient_blog_with_real_intent_does_not_suggest_blog(self, fixtures: tuple[Path, Path]) -> None:
        # ``implement`` is a real intent keyword; the only ``blog`` mention is
        # in the ambient MEMORY.md index. The supplementary blog skill must
        # NOT be suggested (so it never reaches the hard-block set).
        skills_dir, config = fixtures
        suggestions = _suggest(f"implement the loop-tick fix\n{_MEMORY_INDEX_AMBIENT}", skills_dir, config)
        assert "ac-writing-blog-posts" not in suggestions

    def test_ambient_blog_alone_suggests_nothing(self, fixtures: tuple[Path, Path]) -> None:
        # No real intent, only ambient text → vague-prompt short-circuit.
        skills_dir, config = fixtures
        assert _suggest(_MEMORY_INDEX_AMBIENT, skills_dir, config) == []


class TestGenuineBlogKeywordStillSuggested:
    """A real ``blog`` keyword in the prompt body still demands the skill.

    Supplementary skills layer on top of a base lifecycle intent, so the
    enforced scenario is a real task whose own text mentions ``blog`` (here
    ``implement`` drives the ``code`` intent and the in-body ``blog`` word
    triggers the supplementary mapping). The narrowing must not strip a
    keyword that is genuinely part of the task text.
    """

    def test_real_blog_keyword_in_body_suggests_blog_skill(self, fixtures: tuple[Path, Path]) -> None:
        skills_dir, config = fixtures
        suggestions = _suggest(f"implement the new blog post editor\n{_MEMORY_INDEX_AMBIENT}", skills_dir, config)
        assert "ac-writing-blog-posts" in suggestions
