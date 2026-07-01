r"""Select the eval scenarios a PR's changed files define (the selective-PR gate).

A metered behavioral-eval run is expensive (real ``claude`` SDK trials), so a PR
should run only the scenarios it actually touched — never the whole catalog. This
script is the host-workflow shim around that selector: it reads the PR's changed
file paths from STDIN (one repo-relative POSIX path per line, exactly what ``git
diff --name-only`` emits) and delegates to
:func:`teatree.eval.changed_scenarios.select_changed_scenario_names`, the shared
core also exposed as ``t3 eval changed-scenarios`` for overlays to reuse.

Exit 0 when at least one scenario matched (its names were printed) so the eval
runs; exit ``--skip-code`` (default 1) when nothing matched (no scenario file
changed) so the ``eval-pr`` workflow's eval job is skipped cleanly, no API spend.
"""

import argparse
import sys

from teatree.eval.changed_scenarios import names_for_changed, select_changed_scenarios

__all__ = ["main", "names_for_changed"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-code", type=int, default=1, help="Exit code when no scenario file changed.")
    args = parser.parse_args(argv)
    selection = select_changed_scenarios(sys.stdin)
    # Surface the cap when it bites (#2737) so the CI log shows a corpus-wide PR's
    # truncated coverage instead of only the scenarios that will run.
    if note := selection.truncation_note():
        print(note, file=sys.stderr)
    if not selection.names:
        return args.skip_code
    for name in selection.names:
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
