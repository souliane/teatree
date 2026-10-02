"""core.approval_policy (#116): the taint floor beats any dial (RED scenario 4).

The floor is evaluated FIRST and short-circuits, so a forced-permissive dial STILL
returns ASK for an untrusted taint. Only an owner-taint action reaches the dial; in
#116 the empty dial keeps even that ASK.
"""

from teatree.core.models.approval_policy import Decision, approval_policy
from teatree.core.models.provenance import Provenance

_ADMIT = "directive_admit"


def _permissive_dial(_action_class: str) -> Decision:
    return Decision.AUTO_APPROVE


class TestTaintFloor:
    def test_public_taint_asks(self) -> None:
        assert approval_policy(_ADMIT, Provenance.PUBLIC) is Decision.ASK

    def test_web_taint_asks(self) -> None:
        assert approval_policy(_ADMIT, Provenance.WEB) is Decision.ASK

    def test_colleague_taint_asks(self) -> None:
        assert approval_policy(_ADMIT, Provenance.TRUSTED_COLLEAGUE) is Decision.ASK

    def test_unknown_taint_asks_fail_closed(self) -> None:
        assert approval_policy(_ADMIT, "some-unknown-taint") is Decision.ASK

    def test_a_permissive_dial_cannot_override_the_untrusted_floor(self) -> None:
        # THE floor guarantee: even a dial that always AUTO_APPROVEs is short-circuited
        # for an untrusted taint — the floor is checked BEFORE the dial.
        assert approval_policy(_ADMIT, Provenance.PUBLIC, dial=_permissive_dial) is Decision.ASK
        assert approval_policy(_ADMIT, Provenance.WEB, dial=_permissive_dial) is Decision.ASK


class TestOwnerTaintReachesTheDial:
    def test_owner_taint_uses_the_empty_dial_and_asks_in_116(self) -> None:
        # #116 ships the empty dial: owner-taint STILL asks (byte-identical to today).
        assert approval_policy(_ADMIT, Provenance.OWNER) is Decision.ASK

    def test_owner_taint_is_the_only_path_a_dial_can_widen(self) -> None:
        # The owner branch is the ONLY place a future permissive dial takes effect.
        assert approval_policy(_ADMIT, Provenance.OWNER, dial=_permissive_dial) is Decision.AUTO_APPROVE
