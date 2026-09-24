"""headless_admission_block_reason — the headless mirror of the claim gate (#4834).

``claim_admission_block_reason`` (``worker_quiescing`` + schema skew, covered by
``test_managers_quiescing.py``) cannot itself add a ``dispatch``-loop mask check —
``teatree.loops.enable_verdict`` transitively depends back on the leaf's own module
(``models`` -> ``managers`` -> ``managers_task_claim``), so the composition sits one
layer up instead. This is the coverage for that composition: it inherits every
``claim_admission_block_reason`` reason, then adds the ``dispatch`` mask.
"""

import django.test

from teatree.core.headless_admission import headless_admission_block_reason
from teatree.core.mode_resolution import clear_mode_override, set_mode_override
from teatree.core.models import ConfigSetting, LoopState, Mode


class TestHeadlessAdmissionBlockReason(django.test.TestCase):
    def test_admitted_by_default(self) -> None:
        assert headless_admission_block_reason() == ""

    def test_worker_quiescing_still_blocks_it(self) -> None:
        ConfigSetting.objects.set_value("worker_quiescing", value=True)

        assert headless_admission_block_reason() != ""

    def test_mode_override_masking_dispatch_off_blocks_it(self) -> None:
        Mode.objects.create(name="frozen-headless-test", entries={"dispatch": False})
        set_mode_override("frozen-headless-test")

        reason = headless_admission_block_reason()

        assert reason != ""
        assert "dispatch" in reason

    def test_clearing_the_override_re_admits(self) -> None:
        Mode.objects.create(name="frozen-headless-test2", entries={"dispatch": False})
        set_mode_override("frozen-headless-test2")
        assert headless_admission_block_reason() != ""

        clear_mode_override()

        assert headless_admission_block_reason() == ""

    def test_a_durable_hold_on_dispatch_blocks_it_with_no_mode_at_all(self) -> None:
        # The L4 emergency-pause layer, independent of any preset/override — the
        # `dispatch` loop can be held directly without a Mode ever being involved.
        LoopState.objects.pause("dispatch")

        assert headless_admission_block_reason() != ""

    def test_resuming_a_held_dispatch_loop_re_admits(self) -> None:
        LoopState.objects.pause("dispatch")
        assert headless_admission_block_reason() != ""

        LoopState.objects.resume("dispatch")

        assert headless_admission_block_reason() == ""
