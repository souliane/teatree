"""Opt-in worker socket proof that its own agent descendant cannot join attended."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from teatree.agents.live_mailbox import LiveMailboxBroker

_CHILD = """
import sys
from pathlib import Path
from unittest.mock import patch
from teatree.agents.live_mailbox_client import InteractiveMailboxLease, MailboxError
try:
    with patch('teatree.agents.live_mailbox_client._require_trusted_worker'):
        InteractiveMailboxLease.connect(Path(sys.argv[1]))
except MailboxError as exc:
    raise SystemExit(0 if 'permission_denied' in str(exc) else 2)
raise SystemExit(3)
"""


@pytest.mark.integration
def test_worker_descendant_join_returns_permission_denied(tmp_path: Path) -> None:
    if os.environ.get("T3_LIVE_PERMISSION_DENIAL") != "1":
        pytest.skip("set T3_LIVE_PERMISSION_DENIAL=1 for the live permission proof")
    with LiveMailboxBroker(runtime_dir=tmp_path) as broker:
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD, str(tmp_path)], capture_output=True, text=True, timeout=15, check=False
        )
        assert completed.returncode == 0, completed.stderr
        assert broker.identities() == []
