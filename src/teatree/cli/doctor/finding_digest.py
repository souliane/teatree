"""Stable finding IDENTITIES — what makes two watchdog observations "the same finding".

The watchdog DMs the owner on a red doctor verdict and dedups on the notify seam's
idempotency key. That key used to be a hash of the RENDERED body, but several doctor
FAIL lines carry a volatile counter that ticks between passes ("17 commit(s) behind"
→ "18 commit(s) behind", "39 directive item(s) pending"). Every tick minted a fresh
key, so an unchanged condition re-DM'd on every pass forever.

The identity is the message with its volatile numerics normalized away, so an
unchanged condition digests identically pass after pass while a genuine change —
a finding appearing, clearing, or its id-list changing — moves the digest. The
watchdog keys its DM on the digest, which is what turns "post once, re-post on
change" into a property of the key rather than a policy someone must remember.

Deliberately NOT a content hash of the whole body and deliberately NOT age-aware:
the re-surface cadence (a daily heartbeat, a clear-and-return episode) is the
watchdog's ledger to own; this module answers only "is this the same finding?".
"""

import hashlib
import re
from collections.abc import Iterable

#: Every maximal digit run collapses to one placeholder. Counters, ages, id lists and
#: timestamps are the volatile parts of a doctor FAIL line; the surrounding prose is
#: what identifies the finding. A finding whose id LIST changes length still moves the
#: identity (one placeholder per remaining id), which is a real change, not drift.
_DIGITS = re.compile(r"\d+")

#: Truncation of the hex digest. 64 bits is far beyond collision range for the handful
#: of findings one box emits, and a short key keeps the DM ledger readable.
_DIGEST_CHARS = 16


def finding_identity(message: str) -> str:
    """The volatility-normalized identity of one doctor FAIL *message*."""
    return _DIGITS.sub("#", " ".join(message.split()))


def findings_digest(messages: Iterable[str]) -> str:
    """A stable short digest of the SET of finding identities — ``""`` when there are none.

    Set-valued and sorted, so re-ordering the doctor's echoes never re-pages the owner
    while a finding appearing or clearing always does.
    """
    identities = sorted({finding_identity(m) for m in messages if m.strip()})
    if not identities:
        return ""
    joined = "\n".join(identities).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:_DIGEST_CHARS]
