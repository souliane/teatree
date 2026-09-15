"""Unflag the recurrence rows a brief minted against a memory that states no rule (#2663).

The accountant scored every file under the memory dir as a rule and every user-role
transcript line as a correction, so headless dispatch briefs and skill-load turns —
which quote rule prose at length — minted recurrences against per-ticket state logs
and the frontmatter-less index files. Each one drove an umbrella checkbox and a
scheduled coding task to "enforce" a memory that instructs nothing.

Evidence is stored truncated at :data:`_EVIDENCE_CAP`, so a row sitting exactly at the
cap came from a line at least that long — a pasted brief, never a typed correction.
That plus the index-file identities and the unambiguous brief openers is the whole
predicate; a short human correction is left flagged, which is the point.
"""

import re

from django.db import migrations

#: ``detect_compliance_failures`` stored ``line.strip()[:500]``. Frozen here because a
#: migration is a historical artefact — a later cap must not retroactively change it.
_EVIDENCE_CAP = 500

#: Memory-dir files carrying no frontmatter: indexes over the corpus, not memories.
_INDEX_IDENTITIES = frozenset({"MEMORY", "MEMORY_ARCHIVE"})

_ROLE_HEADER_RE = re.compile(r'^\{"role":\s*"\w+"\}\s*')

_BRIEF_OPENERS = (
    "work on ticket",
    "implement github issue",
    "[headless-authoring-ok",
    "you are `",
    "you are the ",
    "you are reviewing",
    "cold review",
    "independent cold review",
    "read-only",
    "<command-message>",
    "<command-name>",
    "<skill-format>",
    "base directory for this skill:",
)

_DISPATCH_FIELDS = ("current phase:", "reason:")


def _is_misattributed(rule_identity: str, evidence: str) -> bool:
    if rule_identity in _INDEX_IDENTITIES:
        return True
    stored = evidence or ""
    # Measure `stored` RAW: the cap was applied to the whole line, role header included,
    # and a truncation that lands on a space is one `.strip()` away from reading 499.
    # Both trims put a capped row under the cap and the truncation signal never fires.
    if len(stored) >= _EVIDENCE_CAP:
        return True
    lowered = _ROLE_HEADER_RE.sub("", stored.strip()).lstrip().lower()
    if lowered.startswith(_BRIEF_OPENERS):
        return True
    return all(field in lowered for field in _DISPATCH_FIELDS)


def unflag_misattributed_recurrences(apps, schema_editor) -> None:
    record = apps.get_model("core", "InstructionComplianceRecord")
    stale = [
        row.pk
        for row in record.objects.filter(is_recurrence=True).only("pk", "rule_identity", "evidence")
        if _is_misattributed(row.rule_identity, row.evidence)
    ]
    for start in range(0, len(stale), 500):
        record.objects.filter(pk__in=stale[start : start + 500]).update(
            is_recurrence=False,
            remediation="none",
            escalation_url="",
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0087_review_head_refresh")]

    operations = [
        migrations.RunPython(unflag_misattributed_recurrences, migrations.RunPython.noop),
    ]
