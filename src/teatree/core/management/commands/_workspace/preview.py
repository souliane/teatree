"""The one dry-run prefix every ``clean-all`` pass renders through.

``clean-all --dry-run`` previews EVERY pass (souliane/teatree#3489): a preview
that under-reports what a destructive command will do is worse than no preview.
Each pass computes its candidate set exactly as a live run would and skips only
the mutation, so the preview and the live run can never disagree about scope.
"""


def preview_line(line: str, *, dry_run: bool) -> str:
    """Prefix *line* with ``WOULD`` under ``dry_run``, else return it unchanged."""
    return f"WOULD {line}" if dry_run else line
