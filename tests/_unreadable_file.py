"""Shared test-infra helper: the precondition for ``chmod(0o000)`` fail-open tests.

A handful of tests prove a reader FAILS OPEN on a file it cannot read, and stage
that by ``chmod(0o000)``-ing the file. That staging only denies the read for an
unprivileged user: root holds ``CAP_DAC_READ_SEARCH``, so the open succeeds, the
reader returns real content, and the assertion inverts. The container CI image
(``python:3.13-bookworm``) runs as uid 0, upstream's ``ubuntu-latest`` runner does
not — which is why these pass upstream and fail here.

``skip_if_root`` is the precise precondition, not a blanket skip: it fires only on
``os.geteuid() == 0``, so the assertion still runs — and still guards the fail-open
branch — for every unprivileged developer and every non-root runner.
"""

import os

import pytest

skip_if_root = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="chmod(0o000) does not deny root (CAP_DAC_READ_SEARCH), so the unreadable-file branch is unreachable",
)
