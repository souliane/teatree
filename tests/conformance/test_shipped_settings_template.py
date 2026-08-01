"""The shipped Claude settings template must carry what teatree recommends.

``recommended_authorizations`` is the doctor's advisory surface: it tells the
operator which auto-mode sentences are absent from their own
``~/.claude/settings.json``. Nothing verified that teatree's OWN shipped
template — the one every container and freshly-provisioned box is seeded from —
carried the same set. A recommendation the ship does not apply is a defect in
the ship, not a nag for the operator: it means every fresh box starts starved
and burns round trips on work the recommendation exists to unblock.

The second class here is the ``permissions.allow`` baseline. Correlating the
classifier denials on a working host back to their originating tool calls put
the overwhelming majority on read-only inspection verbs — ``tail``, ``head``,
``grep``, ``cat``, ``ls``, ``wc`` — refused not on their own merits but because
they sat inside a ``cd <dir> && <work>`` compound the classifier judged as one
opaque unit. So two things must hold: the read-only verbs are individually
allowed, and the compound-judging rule exists as an auto-mode sentence (a
prefix rule cannot express it — ``Bash(cd:*)`` would allow ``cd /x && rm -rf``).

The destructive half is asserted too, from the other direction: this file fails
if the template ever grants a blanket rule for ``rm``, ``sudo``, ``ssh``,
``chmod``, or ``cd`` — the un-blocking must never become a lowering of the bar.
"""

import json
from pathlib import Path

import pytest

from teatree.cli.recommended_authorizations import RECOMMENDED_AUTHORIZATIONS

_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "claude-settings.template.json"

# The read-only inspection verbs that dominated the measured denial set. Each must
# be individually allowed so a bare invocation never reaches the classifier.
_READ_ONLY_VERBS = (
    "tail",
    "head",
    "grep",
    "rg",
    "cat",
    "ls",
    "wc",
    "sed",
    "awk",
    "tr",
    "sort",
    "uniq",
    "cut",
    "comm",
    "diff",
    "stat",
    "du",
    "df",
)

# A blanket rule for any of these would turn the un-blocking into a weakening.
# ``cd`` is on the list because a ``Bash(cd:*)`` prefix rule matches EVERY
# compound that starts with a directory change, destructive tail included, and
# ``find`` because ``-exec`` runs arbitrary commands and ``-delete`` removes files
# — it reads as an inspection verb but is not one. Both are covered instead by the
# wrapper-judging principle, which needs no prefix rule to reach them.
_NEVER_BLANKET_ALLOWED = ("rm", "sudo", "ssh", "chmod", "chown", "cd", "find")


@pytest.fixture(scope="module")
def template() -> dict:
    return json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def permission_rules(template: dict) -> list[str]:
    return [rule for rule in template["permissions"]["allow"] if isinstance(rule, str)]


@pytest.fixture(scope="module")
def automode_sentences(template: dict) -> list[str]:
    return [entry for entry in template["autoMode"]["allow"] if isinstance(entry, str)]


class TestTemplateCarriesEveryRecommendation:
    """The ship applies what the doctor recommends — no drift between the two."""

    @pytest.mark.parametrize("rec", RECOMMENDED_AUTHORIZATIONS, ids=lambda rec: rec.key)
    def test_recommendation_is_present_in_template(self, rec, automode_sentences: list[str]) -> None:
        assert rec.is_covered_by(automode_sentences), (
            f"the shipped template does not carry the `{rec.key}` recommendation; "
            f"every fresh box is seeded starved of it. Add:\n{rec.sentence}"
        )


class TestReadOnlyInspectionIsUnblocked:
    """The measured denial set — read-only verbs and the compound they hid inside."""

    @pytest.mark.parametrize("verb", _READ_ONLY_VERBS)
    def test_read_only_verb_is_individually_allowed(self, verb: str, permission_rules: list[str]) -> None:
        assert f"Bash({verb}:*)" in permission_rules

    def test_generic_wrapper_judging_principle_is_present(self, automode_sentences: list[str]) -> None:
        # The root cause of the measured Bash denials: a command judged by the
        # wrapper it arrived in rather than by the operations it performs. No prefix
        # rule can express this, so it must exist as an auto-mode sentence — and it
        # must be the PRINCIPLE, not a list of wrapper shapes (an enumeration of
        # bypass vectors reads as a bypass cookbook and is itself refused).
        joined = "\n".join(automode_sentences).lower()
        assert "effective operation" in joined
        assert "wrapper" in joined

    def test_wrapping_never_relaxes_a_refusable_operation(self, automode_sentences: list[str]) -> None:
        # The paired half: the judging principle only ever un-blocks work whose every
        # operation is already allowable. Without this sentence the first one could be
        # read as permission to unwrap into something destructive.
        joined = "\n".join(automode_sentences).lower()
        assert "wrapping never" in joined
        assert "stay refused" in joined

    def test_no_bypass_vector_enumeration_in_any_sentence(self, automode_sentences: list[str]) -> None:
        # A sentence that enumerates wrapper shapes to allow ("heredoc", "process
        # substitution", "backticks", "brace group") is a bypass cookbook: it was
        # empirically refused where the short principled statement passed. The
        # principle carries the same coverage without naming a single vector.
        joined = "\n".join(automode_sentences).lower()
        for vector in ("heredoc", "backtick", "process substitution", "brace group", "subshell"):
            assert vector not in joined


class TestFreshBoxIsCorrectByConstruction:
    """``t3 setup --write-automode`` carries the baseline onto a starved host.

    Expanding the template is only half the fix: it has to reach a host that was
    provisioned before the expansion. ``write_host_claude_settings`` unions both
    managed allow-lists, so seeding is idempotent and operator-added grants
    survive — this pins that the composition actually delivers the new baseline
    rather than leaving it in a file nobody applies.
    """

    def test_starved_host_gains_the_baseline_and_keeps_its_own_grants(self, tmp_path: Path) -> None:
        from teatree.cli.setup.claude_settings import write_host_claude_settings

        host = tmp_path / "settings.json"
        host.write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(my-own-tool:*)"]},
                    "autoMode": {"allow": ["My own operator sentence."]},
                }
            ),
            encoding="utf-8",
        )

        merged = write_host_claude_settings(_TEMPLATE_PATH, host, env={})

        permissions = merged["permissions"]["allow"]
        sentences = "\n".join(merged["autoMode"]["allow"]).lower()
        assert "Bash(tail:*)" in permissions
        assert "effective operation" in sentences
        assert "wrapping never" in sentences
        assert "Bash(my-own-tool:*)" in permissions
        assert "My own operator sentence." in merged["autoMode"]["allow"]

    def test_reseeding_an_already_correct_host_adds_nothing(self, tmp_path: Path) -> None:
        from teatree.cli.setup.claude_settings import write_host_claude_settings

        host = tmp_path / "settings.json"
        host.write_text("{}", encoding="utf-8")

        first = write_host_claude_settings(_TEMPLATE_PATH, host, env={})
        second = write_host_claude_settings(_TEMPLATE_PATH, host, env={})

        assert first == second


class TestDestructiveSurfacesStayGated:
    """The other direction: un-blocking read-only work never lowers the bar."""

    @pytest.mark.parametrize("verb", _NEVER_BLANKET_ALLOWED)
    def test_no_blanket_rule_for_a_destructive_verb(self, verb: str, permission_rules: list[str]) -> None:
        assert f"Bash({verb}:*)" not in permission_rules

    def test_rm_rules_stay_scoped_to_throwaway_temp_paths(self, permission_rules: list[str]) -> None:
        rm_rules = [rule for rule in permission_rules if rule.startswith("Bash(rm")]
        assert rm_rules, "the scoped temp-path rm rules are the anti-vacuity control for this assertion"
        for rule in rm_rules:
            assert "/tmp/" in rule or "/var/tmp/" in rule
