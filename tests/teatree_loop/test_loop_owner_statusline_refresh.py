"""``t3 loop claim --take-over`` refreshes the foreign-hijack statusline anchor.

The split-brain bug: the per-session t3-master badge (``statusline.sh``) reads
the LIVE ``loop-registry.json``, while the foreign-hijack RED anchor
(:func:`teatree.loop.phases.render._populate_loop_owner_anchor`) reads the DB
``t3-master`` lease and is only ever re-rendered on a tick or an explicit
re-render. A take-over mutates the DB lease but, pre-fix, never re-rendered the
zones file — so after ``t3 loop claim --slot t3-master --take-over`` the stale
pre-take-over RED line (correctly written while the OLD session owned the lease)
persisted alongside the now-live current-session badge:

    t3-master: <new>·pid<pid> · t3-master=session <old> (NOT this session)

The fix re-renders the statusline from inside the claim command on a won global
``t3-master`` claim, recomputing the anchor against the just-written owner
(which is THIS session), so the stale RED clears in the same command that
transferred ownership.
"""

import io
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import LoopLease
from teatree.loop.phases.render import rerender_statusline
from teatree.loop.statusline import default_path

_RED_MARKER = "(NOT this session)"
_OLD_SESSION = "0cd47d1adeadbeef"
_NEW_SESSION = "647346c8cafebabe"


def _isolated_env(td: str) -> dict[str, str]:
    """Statusline file + loop registry under a temp dir, color stripped.

    ``default_path()`` and the loop-registry both resolve under
    ``XDG_DATA_HOME``; isolating it keeps the rendered file and the session-id
    fallback off the dev box's real ``~/.local/share/teatree``. ``NO_COLOR``
    strips ANSI so the RED-line assertion matches on plain text.
    """
    return {
        "XDG_DATA_HOME": str(Path(td) / "data"),
        "T3_LOOP_REGISTRY_DIR": str(Path(td) / "reg"),
        "NO_COLOR": "1",
    }


def _clear_session_env() -> None:
    for key in ("CLAUDE_SESSION_ID", "T3_LOOP_SESSION_ID", "T3_LOOP_SESSION_PID"):
        os.environ.pop(key, None)


class TestTakeOverRefreshesStatusline(TestCase):
    def test_take_over_clears_stale_foreign_hijack_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, _isolated_env(td)):
            _clear_session_env()

            # 1. The OLD session holds a live ``t3-master`` lease.
            LoopLease.objects.claim_ownership("t3-master", session_id=_OLD_SESSION, owner_pid=os.getpid())

            # 2. The NEW session renders the statusline while the OLD session
            #    still owns it → the foreign-hijack RED line is written.
            os.environ["CLAUDE_SESSION_ID"] = _NEW_SESSION
            rerender_statusline()
            pre = default_path().read_text(encoding="utf-8")
            assert _RED_MARKER in pre  # anti-vacuity: the RED line really is present pre-take-over
            assert _OLD_SESSION[:8] in pre

            # 3. The NEW session takes the loop over via the user hand-off command.
            call_command(
                "loop_owner", "claim", take_over=True, slot="t3-master", json_output=True, stdout=io.StringIO()
            )
            assert LoopLease.objects.get(name="t3-master").session_id == _NEW_SESSION

            # 4. The rendered statusline no longer carries the stale foreign-hijack
            #    line (RED pre-fix — take-over never re-rendered; GREEN post-fix).
            post = default_path().read_text(encoding="utf-8")
            assert _RED_MARKER not in post, f"stale foreign-hijack anchor survived take-over: {post!r}"
            assert _OLD_SESSION[:8] not in post

    def test_genuine_foreign_owner_still_renders_red(self) -> None:
        # A genuinely foreign live owner must still surface the RED anchor — the
        # fix clears a STALE anchor, it must not suppress real hijack detection.
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, _isolated_env(td)):
            _clear_session_env()
            LoopLease.objects.claim_ownership("t3-master", session_id=_OLD_SESSION, owner_pid=os.getpid())
            os.environ["CLAUDE_SESSION_ID"] = _NEW_SESSION

            rerender_statusline()

            rendered = default_path().read_text(encoding="utf-8")
            assert _RED_MARKER in rendered
            assert f"t3-master=session {_OLD_SESSION[:8]} {_RED_MARKER}" in rendered
