"""The shipped doctrine and the eval corpus grade the same command.

The whole-system property behind souliane/teatree#4137. ``skills/ship/SKILL.md``
§ 4a made ``t3 push`` the one supported push path from the worker container in
#3949; three matchers kept asserting the pre-#3949 ``git push`` spelling, so for
months an agent that followed current doctrine EXACTLY was graded wrong. The skill
is correct on its own and every matcher is correct on its own — the defect is the
relationship, which is exactly the shape no unit test can see.

Two directions, both derived from the same *seam migration* structure (a doctrine
block whose forbidden and mandated forms are different PROGRAMS): no positive
matcher may demand a command its own doctrine retired, and every mandated command
must be pinned by at least one scenario graded against that doctrine.

**Each direction grades an exact, named set — never a "no worse than" floor.**
A NEW offender fails this test, and closing a recorded one fails it too until
that entry is deleted. Deleting the entry is part of the fix, not a chore left
behind by it.
"""

from pathlib import Path

from teatree.eval.doctrine_agreement import (
    SeamMigration,
    seam_migrations,
    shipped_seam_migrations,
    shipped_specs,
    stale_matchers,
    unpinned_mandates,
)
from teatree.eval.models import EvalSpec, Matcher

#: No stale matcher is open. A matcher that demands a retired command is fixed
#: where it is GENERATED (``scripts/eval/corpus_gen/``), never recorded here —
#: an entry is a temporary record of a known-open offender, not an allowlist.
_KNOWN_STALE_MATCHERS: frozenset[str] = frozenset()

#: The re-enable half of the gate kill-switch. ``t3 <overlay> gate disable`` IS
#: pinned; nothing grades the restore, so a doctrine change to the re-enable
#: spelling would go unnoticed the same way #4137 did.
_KNOWN_UNPINNED_MANDATES = frozenset({"skills/workspace/SKILL.md: 't3 <overlay> gate enable' is pinned by no scenario"})

#: Anti-vacuity floors. Far below today's real sizes; they exist so an emptied
#: doctrine corpus or an emptied scenario catalog cannot make a lane pass by
#: having nothing to check.
_MIN_SEAM_MIGRATIONS = 1
_MIN_SPECS = 100

_SHIP_SEAM = """```bash
# do X — the supported seam:
t3 push
t3 push --force-with-lease

# never Y — a bare push from a container shell:
git push -u origin HEAD                       # FORBIDDEN from a container shell
```"""

_ARGUMENT_LEVEL_PAIR = """```bash
gh pr create --base main --head 42-fix --fill
gh pr create --base main --head 42-fix --fill --draft   # FORBIDDEN by default
```"""


class TestLiveCorpusAgreesWithLiveDoctrine:
    def test_the_doctrine_corpus_is_not_empty(self) -> None:
        assert len(shipped_seam_migrations()) >= _MIN_SEAM_MIGRATIONS

    def test_the_scenario_catalog_is_not_empty(self) -> None:
        assert len(shipped_specs()) >= _MIN_SPECS

    def test_no_matcher_demands_a_command_its_doctrine_retired(self) -> None:
        found = {str(v) for v in stale_matchers(shipped_specs(), shipped_seam_migrations())}
        assert found == _KNOWN_STALE_MATCHERS

    def test_every_mandated_command_is_pinned_by_a_scenario(self) -> None:
        found = {str(v) for v in unpinned_mandates(shipped_specs(), shipped_seam_migrations())}
        assert found == _KNOWN_UNPINNED_MANDATES


class TestSeamMigrationExtraction:
    """A migration is a PROGRAM change; an argument-level pair is not one."""

    def test_a_cross_program_block_is_a_migration(self) -> None:
        (block,) = seam_migrations("skills/ship/SKILL.md", _SHIP_SEAM)
        assert block.mandated == ("t3 push", "t3 push --force-with-lease")
        assert block.forbidden == ("git push -u origin HEAD",)

    def test_a_same_program_block_is_not_a_migration(self) -> None:
        assert seam_migrations("skills/rules/SKILL.md", _ARGUMENT_LEVEL_PAIR) == ()

    def test_prose_and_tool_call_pseudocode_are_not_commands(self) -> None:
        text = "```bash\nt3 push\nEdit(file_path='x.py')   # FORBIDDEN in the main agent\n```"
        assert seam_migrations("skills/rules/SKILL.md", text) == ()


class TestStaleMatcherDetectionIsAntiVacuous:
    """The detector must go RED on the real pre-#4109 matcher and GREEN on its fix."""

    _MIGRATION = SeamMigration(
        skill="skills/ship/SKILL.md",
        mandated=("t3 push", "t3 push --force-with-lease"),
        forbidden=("git push -u origin HEAD",),
    )

    def _stale(self, pattern: str) -> tuple[str, ...]:
        spec = _spec_with_command_matcher(pattern)
        return tuple(v.pattern for v in stale_matchers((spec,), (self._MIGRATION,)))

    def test_the_retired_spelling_is_flagged(self) -> None:
        assert self._stale(r"git push .*(-u )?origin (?!main\b)\S") == (r"git push .*(-u )?origin (?!main\b)\S",)

    def test_the_widened_spelling_is_not_flagged(self) -> None:
        assert self._stale(r"(git|t3) push") == ()

    def test_a_matcher_that_names_only_the_supported_seam_is_not_flagged(self) -> None:
        assert self._stale(r"t3 push") == ()

    def test_a_matcher_on_an_unrelated_command_is_not_flagged(self) -> None:
        assert self._stale(r"gh pr create") == ()

    def test_a_mandated_command_with_no_matcher_is_unpinned(self) -> None:
        spec = _spec_with_command_matcher(r"t3 push$")  # pins the bare form only
        assert tuple(v.command for v in unpinned_mandates((spec,), (self._MIGRATION,))) == (
            "t3 push --force-with-lease",
        )


def _spec_with_command_matcher(pattern: str) -> EvalSpec:
    """A one-matcher scenario graded against the ship doctrine."""
    return EvalSpec(
        name="probe",
        scenario="probe",
        agent_path="skills/ship/SKILL.md",
        prompt="probe",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="args.command", operator="~", value=pattern),),
        source_path=Path("<probe>"),
    )
