"""``manage.py loop_runner`` — run the self-owned singleton loop-runner daemon (#2876).

Acquires the ``loop-runner`` flock singleton (:func:`teatree.utils.singleton.singleton`)
so at most one runner exists per box, then runs the supervised beat daemon
(:class:`teatree.loops.runner.LoopRunnerDaemon`). A second invocation while a
runner is alive refuses immediately with a non-zero exit (the ``flock`` is the
lock; the pid in the file is diagnostic only). ``t3 loop-runner --once`` runs a
single beat and returns — the foreground / test variant that supersedes the removed
foreground ``loops run`` continuous runner (#2880).
"""

from typing import Annotated

import typer
from django_typer.management import TyperCommand


class Command(TyperCommand):
    help = "Run the self-owned singleton loop-runner daemon (#2876) — owns the tick cadence, no OS cron."

    def handle(
        self,
        *,
        once: Annotated[
            bool, typer.Option("--once", help="Run a single beat then exit (foreground / test variant).")
        ] = False,
    ) -> None:
        from teatree.loops.runner import LOOP_RUNNER_SINGLETON, LoopRunnerDaemon  # noqa: PLC0415
        from teatree.utils.singleton import AlreadyRunningError, singleton  # noqa: PLC0415

        try:
            with singleton(LOOP_RUNNER_SINGLETON):
                daemon = LoopRunnerDaemon()
                daemon.run_once() if once else daemon.run()
        except AlreadyRunningError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(1) from exc
