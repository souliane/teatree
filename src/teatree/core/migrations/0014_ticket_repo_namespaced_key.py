import re
from urllib.parse import urlparse

from django.db import migrations, models

# Frozen copy of `teatree.utils.url_slug`'s issue-only regexes (#2293): a
# migration's data transform must stay historically accurate even if the
# live parser's rules evolve later, so the pattern is duplicated here
# rather than imported (mirrors `0005_backfill_task_subject._ticket_number`).
_GITHUB_ISSUE_RE = re.compile(r"^/(?P<slug>[^/]+/[^/]+)/issues/(?P<number>\d+)/?$")
_GITLAB_ISSUE_RE = re.compile(r"^/(?P<slug>.+?)/-/(?:issues|work_items)/(?P<number>\d+)/?$")


def _repo_namespaced_key(issue_url: str) -> str:
    path = urlparse(issue_url or "").path
    gitlab = _GITLAB_ISSUE_RE.match(path)
    if gitlab is not None:
        return f"{gitlab['slug']}#{gitlab['number']}"
    github = _GITHUB_ISSUE_RE.match(path)
    if github is not None:
        return f"{github['slug']}#{github['number']}"
    return ""


def backfill_repo_namespaced_key(apps, schema_editor):
    Ticket = apps.get_model("core", "Ticket")
    # Seed with every already-set key (an explicit value, or a prior partial
    # run of this backfill) so a freshly-computed key can never claim one
    # that is already spoken for — this is idempotent and re-runnable.
    seen: set[str] = set(Ticket.objects.exclude(repo_namespaced_key="").values_list("repo_namespaced_key", flat=True))
    candidates = Ticket.objects.filter(repo_namespaced_key="").exclude(issue_url="")
    for ticket in candidates.iterator():
        key = _repo_namespaced_key(ticket.issue_url)
        # No-op for a non-issue `issue_url` (blank key) and for a duplicate
        # key a pre-existing data anomaly would otherwise produce (two rows
        # whose `issue_url` differ byte-for-byte but parse to the same repo
        # and issue number) — the unique constraint added below must never
        # see an IntegrityError from this backfill.
        if not key or key in seen:
            continue
        seen.add(key)
        ticket.repo_namespaced_key = key
        ticket.save(update_fields=["repo_namespaced_key"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0013_alter_pendingchatinjection_answer_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="repo_namespaced_key",
            field=models.CharField(blank=True, db_index=True, default="", max_length=300),
        ),
        migrations.RunPython(backfill_repo_namespaced_key, noop_reverse),
        migrations.AddConstraint(
            model_name="ticket",
            constraint=models.UniqueConstraint(
                condition=models.Q(("repo_namespaced_key", ""), _negated=True),
                fields=("repo_namespaced_key",),
                name="unique_nonempty_repo_namespaced_key",
            ),
        ),
    ]
