"""``manage.py loop_verify_cron <name>`` — verify-by-reread a loop's cron registration (#1192).

Judges a ``CronList`` JSON snapshot the caller supplies (a CLI cannot call the
harness itself) against the loop's expected native Claude ``/loop`` spec.
"""

import io
import json
import tempfile
from pathlib import Path

import django.test
import pytest
from django.core.management import call_command

from teatree.core.models import Loop, Prompt


def _prompt(name: str = "demo-prompt") -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name=name, defaults={"body": "do x"})
    return prompt


def _run(*args: str, **kwargs: object) -> str:
    out = io.StringIO()
    err = io.StringIO()
    call_command("loop_verify_cron", *args, stdout=out, stderr=err, **kwargs)
    return out.getvalue() + err.getvalue()


class _SnapshotFiles:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._counter = 0

    def write(self, content: str) -> str:
        self._counter += 1
        path = self._directory / f"snapshot-{self._counter}.json"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def write_json(self, jobs: object) -> str:
        return self.write(json.dumps(jobs))


class TestLoopVerifyCronCommand(django.test.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.snapshots = _SnapshotFiles(Path(self._tmpdir.name))
        self.addCleanup(self._tmpdir.cleanup)

    def test_unknown_loop_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            _run("nope", cron_list_json=self.snapshots.write_json([]))

    def test_confirmed_registration_exits_zero_and_names_the_slot(self) -> None:
        Loop.objects.create(name="vc-ship", delay_seconds=300, prompt=_prompt(), enabled=True)
        snapshot = self.snapshots.write_json(
            [
                {
                    "id": "job-1",
                    "prompt": "Run `t3 loops tick --loop vc-ship` in Bash, then briefly report the tick summary.",
                }
            ],
        )
        out = _run("vc-ship", cron_list_json=snapshot)
        assert "confirmed" in out
        assert "t3-loop-vc-ship" in out

    def test_missing_registration_exits_nonzero(self) -> None:
        Loop.objects.create(name="vc-review", delay_seconds=300, prompt=_prompt(), enabled=True)
        with pytest.raises(SystemExit):
            _run("vc-review", cron_list_json=self.snapshots.write_json([]))

    def test_prefix_name_does_not_false_confirm(self) -> None:
        # `vc-ship` must not be confirmed by a `vc-ship-fast` job's prompt.
        Loop.objects.create(name="vc-ship", delay_seconds=300, prompt=_prompt(), enabled=True)
        snapshot = self.snapshots.write_json(
            [{"prompt": "Run `t3 loops tick --loop vc-ship-fast` in Bash, then briefly report the tick summary."}],
        )
        with pytest.raises(SystemExit):
            _run("vc-ship", cron_list_json=snapshot)

    def test_malformed_json_exits_nonzero(self) -> None:
        Loop.objects.create(name="vc-bad", delay_seconds=300, prompt=_prompt(), enabled=True)
        with pytest.raises(SystemExit):
            _run("vc-bad", cron_list_json=self.snapshots.write("not json"))

    def test_non_array_json_exits_nonzero(self) -> None:
        Loop.objects.create(name="vc-wrap", delay_seconds=300, prompt=_prompt(), enabled=True)
        with pytest.raises(SystemExit):
            _run("vc-wrap", cron_list_json=self.snapshots.write_json({"crons": []}))
