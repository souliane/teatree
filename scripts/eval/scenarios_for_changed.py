r"""Select the eval scenarios a PR's changed files define (the selective-PR gate).

A metered behavioral-eval run is expensive (real ``claude`` SDK trials), so a PR
should run only the scenarios it actually touched — never the whole catalog. This
script is the host-workflow shim around that selector: it reads the PR's changed
file paths from STDIN (one repo-relative POSIX path per line, exactly what ``git
diff --name-only`` emits) and delegates to
:func:`teatree.eval.changed_scenarios.select_changed_scenarios`, the shared
core also exposed as ``t3 eval changed-scenarios`` for overlays to reuse.

``--diff-file`` adds the prose granularity STDIN cannot carry (#3944): a skill
file's path says only THAT it changed, so every scenario grading any of its ~70
sections would answer alike. Given the same range's ``git diff --unified=0``
output it narrows a section-scoped scenario to the sections that actually moved.
Paths still come from STDIN, so a diff the parser cannot read costs precision,
never a missed scenario.

A scenario in the known-red quarantine (``evals/quarantine.yaml``) is dropped from
the selection and NAMED on stderr (#4173), so a tracked red stops blocking every
PR touching the prose it grades without the shrunken lane going unnoticed.

``--repo-root`` names the root the STDIN paths are relative to, for a core vendored
below its consuming repo. ``--metadata-env`` writes ``EVAL_DEFERRED=<n>`` (the count
the cap deferred) as a dotenv file, so a lane can report incomplete coverage.

Exit 0 when at least one scenario matched (its names were printed) so the eval
runs; exit ``--skip-code`` (default 1) when nothing matched (no scenario file
changed) so the ``eval-pr`` workflow's eval job is skipped cleanly, no API spend.
"""

import argparse
import sys
from pathlib import Path

from teatree.eval.changed_scenarios import REPO_ROOT, names_for_changed, select_changed_scenarios
from teatree.eval.changed_sections import changed_sections_by_path

__all__ = ["main", "names_for_changed"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-code", type=int, default=1, help="Exit code when no scenario file changed.")
    parser.add_argument(
        "--diff-file",
        type=Path,
        default=None,
        help="Unified diff (git diff --unified=0) for the same range, to narrow section-scoped scenarios.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Root the STDIN paths are relative to (a consuming repo's, when core is vendored).",
    )
    parser.add_argument("--metadata-env", type=Path, default=None, help="Write EVAL_DEFERRED=<n> to this dotenv file.")
    args = parser.parse_args(argv)
    sections = None
    if args.diff_file is not None:
        sections = changed_sections_by_path(args.diff_file.read_text(encoding="utf-8"), repo_root=args.repo_root)
    selection = select_changed_scenarios(sys.stdin, repo_root=args.repo_root, changed_sections=sections)
    if args.metadata_env is not None:
        args.metadata_env.write_text(f"EVAL_DEFERRED={selection.deferred}\n", encoding="utf-8")
    # Surface the cap when it bites (#2737) so the CI log shows a corpus-wide PR's
    # truncated coverage instead of only the scenarios that will run.
    if note := selection.truncation_note():
        print(note, file=sys.stderr)
    # Same reason for a quarantine suppression (#4173) — a shrunken lane must be visible.
    if note := selection.quarantine_note():
        print(note, file=sys.stderr)
    if not selection.names:
        return args.skip_code
    for name in selection.names:
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
