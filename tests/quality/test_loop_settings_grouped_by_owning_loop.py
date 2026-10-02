# test-path: cross-cutting
"""A loop's settings are declared under that loop, and the source is what says so.

B17: a loop that is off makes its own settings moot BY CONSTRUCTION, so "which settings
belong to ``dream``" has to be the shape of the config rather than a table someone keeps
in step by hand. The shape here is the declaration base a field sits in; this gate is
what stops that shape drifting from the code, by re-deriving ownership from the read
sites on every run (:mod:`teatree.quality.loop_setting_ownership`).

Both directions are asserted, because each catches a different rot: a key whose reader
moved INTO a loop and was left in a generic group, and a key filed under a loop that
never reads it — the mis-grouping a name-prefix map produces.
"""

import dataclasses
import importlib
from pathlib import Path

import teatree
from teatree.config.setting_groups import setting_group_path
from teatree.config.settings import UserSettings
from teatree.config.settings_loop_owned import LOOP_OWNED_SETTING_BASES
from teatree.quality.loop_setting_ownership import LoopLayerIndex, loop_owned_settings

_SOURCE_ROOT = Path(teatree.__file__).parent

#: The top level every per-loop group hangs under, and the label a loop group carries.
_LOOPS = "Loops"


def _schema_keys() -> tuple[str, ...]:
    return tuple(f.name for f in dataclasses.fields(UserSettings))


def _owned() -> dict[str, str]:
    return loop_owned_settings(_SOURCE_ROOT, _schema_keys())


#: Keys a ``review``-prefix map claims for the review loop; every one is read by a
#: review GATE or by the on-behalf chokepoint, outside any loop's own code.
_PREFIX_LOOKALIKES = (
    "review_backend_cooldown_hours",
    "review_exempt_repos",
    "review_request_post_disabled",
    "review_skill",
    "review_skill_alternates",
)


class TestTheDeclaredBaseListIsComplete:
    """``LOOP_OWNED_SETTING_BASES`` names EVERY per-loop base the module defines.

    The tuple is what the grouping renders from, so a base added and left out of it
    declares settings that render under no loop at all — visible nowhere the operator
    looks for them, which is the shape B12 exists to remove. Nothing else read this
    tuple, so nothing would have noticed.
    """

    def test_every_per_loop_base_defined_here_is_listed(self) -> None:
        module = importlib.import_module("teatree.config.settings_loop_owned")
        defined = {
            value
            for name, value in vars(module).items()
            if isinstance(value, type) and name.endswith("LoopSettings") and hasattr(value, "GROUP_PATH")
        }

        assert defined, "no per-loop bases found — the naming this walk keys on has moved"
        assert set(LOOP_OWNED_SETTING_BASES) == defined, (
            "a base is defined but unlisted (its settings render under no loop), or listed "
            "but gone: " + str({b.__name__ for b in set(LOOP_OWNED_SETTING_BASES) ^ defined})
        )

    def test_each_listed_base_groups_under_the_loops_tree(self) -> None:
        for base in LOOP_OWNED_SETTING_BASES:
            assert base.GROUP_PATH[0] == _LOOPS, f"{base.__name__} does not group under {_LOOPS}"
            assert len(base.GROUP_PATH) == 2, f"{base.__name__} must name exactly one loop"


class TestTheDerivationDiscriminates:
    """The control: the map answers from read sites, so a lookalike NAME does not fool it."""

    def test_a_prefix_lookalike_is_not_claimed_by_the_loop_it_names(self) -> None:
        owned = _owned()
        assert [key for key in _PREFIX_LOOKALIKES if key in owned] == []
        # The control: a key carrying no `review` in its name IS claimed for the review
        # loop, because that is where its reader lives — so the derivation answers from
        # read sites rather than blanket-refusing a prefix.
        assert owned["admit_colleague_prs_to_board"] == "review"

    def test_the_derivation_finds_the_loops_whose_settings_it_can_see(self) -> None:
        owned = _owned()
        assert owned, "the derivation found no loop-owned setting at all — the probe is broken"
        assert {owned[key] for key in owned if key.startswith("dream_")} == {"dream"}


class TestEveryLoopOwnedKeyIsDeclaredUnderItsLoop:
    def test_each_owned_key_renders_under_its_own_loops_group(self) -> None:
        misplaced = {
            key: (setting_group_path(key), (_LOOPS, loop))
            for key, loop in _owned().items()
            if setting_group_path(key) != (_LOOPS, loop)
        }
        assert not misplaced, f"read inside one loop, declared elsewhere: {misplaced}"


class TestNoKeyIsFiledUnderALoopThatDoesNotReadIt:
    def test_each_per_loop_group_holds_exactly_its_own_traced_keys(self) -> None:
        loops = set(LoopLayerIndex(_SOURCE_ROOT).loop_names)
        owned = _owned()
        declared: dict[str, set[str]] = {}
        for key in _schema_keys():
            path = setting_group_path(key)
            if len(path) == 2 and path[0] == _LOOPS and path[1] in loops:
                declared.setdefault(path[1], set()).add(key)
        traced: dict[str, set[str]] = {}
        for key, loop in owned.items():
            traced.setdefault(loop, set()).add(key)
        assert declared == traced
