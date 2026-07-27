# test-path: cross-cutting — drives deploy/watchdog.sh (no src mirror).
"""The watchdog's re-surface policy (deploy/watchdog.sh).

The owner's ledger held 192 copies of ONE unchanged finding set, because the DM's
idempotency key was a hash of the RENDERED body and several doctor FAIL lines carry a
counter that ticks between passes ("17 commit(s) behind" → "18 …"). Every tick minted a
fresh key, so the notify seam's dedup never fired.

The policy these pin: key on the FINDING IDENTITIES, not the rendered text; bump an
episode whenever the box goes green, so a set that clears and returns pages again; add a
day bucket, so an unchanged finding still re-surfaces once a day and the watchdog is
never silent; and DM once when the findings clear.

Runs the REAL ``run_pass`` (the script is sourced, its dispatch guarded so it does not
auto-run) with a stub ``docker`` modelling the compose calls. Nothing is posted anywhere:
the ``notify send`` exec is captured to a file.
"""

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from teatree.cli.doctor.finding_digest import finding_identity

WATCHDOG = Path(__file__).resolve().parents[1] / "deploy" / "watchdog.sh"
_BASH = shutil.which("bash") or "bash"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="needs bash + python3 (present in the deploy image and CI)",
)

_SKILL_FAIL = "/opt/agent-skills/ac-reviewing-codebase/SKILL.md: requires unknown skill 'review'"
_WORKER_FAIL = "Compose service teatree-worker is exited"
_GREEN = '{"ok": true, "findings": []}'


def _directive_fail(pending: int) -> str:
    """The real shape of a finding whose message carries a counter that ticks every pass."""
    return f"{pending} directive item(s) pending but the 'directive_loop' consumer is not live"


def _verdict(*messages: str) -> str:
    """A doctor verdict shaped exactly as the real one, identities included.

    The identity comes from the production normalizer rather than a hand-written string,
    so this harness cannot drift from what the worker actually emits.
    """
    return _verdict_with_identities(*((m, finding_identity(m)) for m in messages))


def _verdict_with_identities(*pairs: tuple[str, str]) -> str:
    findings = ", ".join(f'{{"level": "FAIL", "message": "{m}", "identity": "{i}"}}' for m, i in pairs)
    return f'{{"ok": false, "findings": [{findings}]}}'


@dataclass(frozen=True, slots=True)
class Dm:
    """One captured owner DM — its idempotency key and its body."""

    key: str
    body: str


def _write_docker_stub(bin_dir: Path) -> None:
    """A ``docker`` shim modelling the compose calls ``run_pass`` makes.

    A doctor ``exec`` prints ``STUB_DOCTOR_JSON``; a ``notify send`` exec appends a
    tab-separated ``<key>`` and ``<body>`` (newlines folded) to ``STUB_NOTIFY_LOG``, so a
    multi-pass run can assert on the KEY sequence — what the notify seam actually dedups on.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" != compose ]; then exit 0; fi\n'
        "shift\n"
        'while [ "${1:-}" = -p ] || [ "${1:-}" = -f ]; do shift 2; done\n'
        'sub="${1:-}"; shift || true\n'
        'case "$sub" in\n'
        '  ps) printf "%s\\n" \'{"State":"exited","ExitCode":0}\' ;;\n'
        "  up) exit 0 ;;\n"
        "  exec)\n"
        '    [ "${1:-}" = -T ] && shift\n'
        '    while [ "${1:-}" = -e ]; do shift 2; done\n'
        "    shift || true\n"
        '    case "$*" in\n'
        "      true) exit 0 ;;\n"
        '      *"doctor check --json"*) printf "%s\\n" "$STUB_DOCTOR_JSON"; exit 1 ;;\n'
        '      *"notify send"*)\n'
        '        argv="$*"\n'
        '        body="$(cat | tr "\\n" " ")"\n'
        '        printf "%s\\t%s\\n" "${argv##* }" "$body" >>"$STUB_NOTIFY_LOG"\n'
        "        exit 0 ;;\n"
        "      *) exit 0 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class Watchdog:
    """Drives consecutive ``run_pass`` invocations that share one durable ledger."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.notify_log = tmp_path / "notify.log"
        _write_docker_stub(tmp_path / "bin")
        harness = tmp_path / "harness.sh"
        harness.write_text(f'set -uo pipefail\nsource "{WATCHDOG}"\nrun_pass\n', encoding="utf-8")
        self.harness = harness

    def run(self, verdict: str, *, day: str = "20260101") -> None:
        env = dict(os.environ)
        env["PATH"] = f"{self.tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
        env["STUB_DOCTOR_JSON"] = verdict
        env["STUB_NOTIFY_LOG"] = str(self.notify_log)
        env["TEATREE_WATCHDOG_DAY_BUCKET"] = day
        env["TEATREE_WATCHDOG_DEPLOY_PENDING_STATE"] = str(self.tmp_path / "pending.state")
        env["TEATREE_WATCHDOG_RED_STATE"] = str(self.tmp_path / "red.state")
        env["TEATREE_WATCHDOG_DEPLOY_LOCK"] = str(self.tmp_path / "absent-deploy.lock")
        subprocess.run([_BASH, str(self.harness)], capture_output=True, text=True, check=False, env=env)

    @property
    def dms(self) -> list[Dm]:
        if not self.notify_log.exists():
            return []
        lines = self.notify_log.read_text(encoding="utf-8").splitlines()
        return [Dm(*line.split("\t", 1)) for line in lines if line.strip()]

    @property
    def keys(self) -> list[str]:
        return [dm.key for dm in self.dms]


class TestUnchangedFindingsDoNotRePage:
    def test_a_ticking_counter_reuses_the_same_idempotency_key(self, tmp_path: Path) -> None:
        """The measured bug: 39 → 40 pending directives re-DM'd an unchanged condition."""
        watchdog = Watchdog(tmp_path)
        watchdog.run(_verdict(_SKILL_FAIL, _directive_fail(39)))
        watchdog.run(_verdict(_SKILL_FAIL, _directive_fail(40)))

        assert len(watchdog.keys) == 2, "both passes must attempt a DM; dedup is the seam's job"
        assert watchdog.keys[0] == watchdog.keys[1]

    def test_finding_order_does_not_change_the_key(self, tmp_path: Path) -> None:
        watchdog = Watchdog(tmp_path)
        watchdog.run(_verdict(_SKILL_FAIL, _WORKER_FAIL))
        watchdog.run(_verdict(_WORKER_FAIL, _SKILL_FAIL))

        assert watchdog.keys[0] == watchdog.keys[1]


class TestChangedFindingsDoPage:
    def test_an_added_finding_mints_a_new_key(self, tmp_path: Path) -> None:
        watchdog = Watchdog(tmp_path)
        watchdog.run(_verdict(_SKILL_FAIL))
        watchdog.run(_verdict(_SKILL_FAIL, _WORKER_FAIL))

        assert watchdog.keys[0] != watchdog.keys[1]

    def test_a_finding_that_clears_and_returns_pages_again(self, tmp_path: Path) -> None:
        """Never silence: the same set after a green pass is a NEW incident, not a repeat."""
        watchdog = Watchdog(tmp_path)
        watchdog.run(_verdict(_SKILL_FAIL))
        watchdog.run(_GREEN)
        watchdog.run(_verdict(_SKILL_FAIL))

        red_keys = [key for key in watchdog.keys if key.startswith("watchdog:red:")]
        assert len(red_keys) == 2
        assert red_keys[0] != red_keys[1]

    def test_an_unchanged_finding_re_surfaces_on_the_next_day(self, tmp_path: Path) -> None:
        """The deliberate long interval — an unchanged red never goes permanently quiet."""
        watchdog = Watchdog(tmp_path)
        watchdog.run(_verdict(_SKILL_FAIL), day="20260101")
        watchdog.run(_verdict(_SKILL_FAIL), day="20260102")

        assert watchdog.keys[0] != watchdog.keys[1]


class TestClearIsAnnouncedOnce:
    def test_going_green_after_red_dms_the_clear(self, tmp_path: Path) -> None:
        watchdog = Watchdog(tmp_path)
        watchdog.run(_verdict(_SKILL_FAIL))
        watchdog.run(_GREEN)

        cleared = [dm for dm in watchdog.dms if dm.key.startswith("watchdog:cleared:")]
        assert len(cleared) == 1
        assert "CLEARED" in cleared[0].body

    def test_a_second_green_pass_says_nothing(self, tmp_path: Path) -> None:
        watchdog = Watchdog(tmp_path)
        watchdog.run(_verdict(_SKILL_FAIL))
        watchdog.run(_GREEN)
        watchdog.run(_GREEN)

        assert len([dm for dm in watchdog.dms if dm.key.startswith("watchdog:cleared:")]) == 1

    def test_a_green_box_that_was_never_red_says_nothing(self, tmp_path: Path) -> None:
        watchdog = Watchdog(tmp_path)
        watchdog.run(_GREEN)

        assert watchdog.dms == []


class TestBodyStillCarriesTheFindings:
    def test_the_dm_body_lists_the_messages_not_the_identities(self, tmp_path: Path) -> None:
        """The identity is a dedup key; the owner must still read the real message."""
        watchdog = Watchdog(tmp_path)
        watchdog.run(_verdict_with_identities((_directive_fail(39), _directive_fail(0))))

        body = watchdog.dms[0].body
        assert "39 directive item(s) pending" in body
        assert "\t" not in body

    def test_a_doctor_without_identities_still_pages(self, tmp_path: Path) -> None:
        """Rolling-deploy safety: an older doctor emits no ``identity`` — fall back to the message."""
        watchdog = Watchdog(tmp_path)
        watchdog.run(f'{{"ok": false, "findings": [{{"level": "FAIL", "message": "{_WORKER_FAIL}"}}]}}')

        assert len(watchdog.dms) == 1
        assert _WORKER_FAIL in watchdog.dms[0].body
