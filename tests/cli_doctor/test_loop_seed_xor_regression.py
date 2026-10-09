# test-path: cross-cutting
"""Regression guard for the `loop_prompt_xor_script` shuffle-order flake.

Under ``pytest-randomly`` a preceding ``TransactionTestCase`` flushes ``teatree_loop``
WITHOUT restoring the migration-seeded rows (no ``serialized_rollback``), so a later
test's ``Loop.objects.update_or_create(name="inbox", …)`` can hit the CREATE branch on
an EMPTY table. A bare ``defaults={"enabled": True}`` create then builds a ``Loop`` with
neither ``prompt`` nor ``script`` set — tripping the ``loop_prompt_xor_script`` CHECK
constraint. The consuming ``setUp`` (``tests/cli_doctor/test_slack_roundtrip.py``) now
passes ``create_defaults`` that seed the same script-backed shape the migration uses; this
module pins BOTH halves so a future revert reds deterministically instead of flaking.
"""

import pytest
from django.db import IntegrityError, transaction
from django.test import TestCase

from teatree.core.models import Loop

# The migration-seeded shape of the script-backed `inbox` loop
# (src/teatree/core/migrations/0001_initial.py): prompt is null, script points at the
# loop module, delay is set — the only XOR-valid create for a script-backed loop.
_INBOX_CREATE_DEFAULTS = {
    "enabled": True,
    "script": "src/teatree/loops/inbox/loop.py",
    "delay_seconds": 60,
}


class TestInboxSeedOnFlushedLoopTable(TestCase):
    def test_bare_defaults_create_violates_the_xor_constraint(self) -> None:
        # Documents WHY create_defaults is required: the bare create is XOR-invalid.
        Loop.objects.all().delete()
        with pytest.raises(IntegrityError), transaction.atomic():
            Loop.objects.update_or_create(name="inbox", defaults={"enabled": True})

    def test_create_defaults_keep_the_create_branch_xor_valid(self) -> None:
        Loop.objects.all().delete()
        loop, created = Loop.objects.update_or_create(
            name="inbox",
            defaults={"enabled": True},
            create_defaults=_INBOX_CREATE_DEFAULTS,
        )
        assert created
        assert loop.enabled
        assert loop.script
        assert loop.prompt is None
