"""Surface the user's OWN open authored MRs that are in merge conflict.

The followup sweep already fetches the author's open MRs (to upsert tickets);
this reads each raw MR's conflict signals and translates the conflicted ones
into the overlay-agnostic :class:`~teatree.types.ConflictedMR` shape so
``sync_followup`` can surface them loudly. Detection / reporting only — never
an auto-resolve or auto-push (#78). A conflicted MR re-conflicts as master
advances, so it must be re-checked every sweep rather than cached.

GitLab method/field names that mirror the literal ``/merge_requests``
endpoint keep their GitLab-native naming.
"""

from typing import SupportsInt, cast

from teatree.backends.gitlab.sync_prs import extract_repo_path
from teatree.types import ConflictedMR, RawAPIDict, SyncResult

#: GitLab ``merge_status`` value for a hard conflict. The field is deprecated
#: in favour of ``detailed_merge_status`` but is still returned on the MR list
#: payload, so both are read (either signal flags the MR as conflicted).
_MERGE_STATUS_CONFLICT = "cannot_be_merged"

#: GitLab ``detailed_merge_status`` value for a hard merge conflict (16.x+).
#: ``broken_status`` / ``ci_must_pass`` / ``not_approved`` are NOT conflicts —
#: only a genuine ``conflict`` is surfaced, so the warning never cries wolf.
_DETAILED_MERGE_STATUS_CONFLICT = "conflict"


def is_conflicted(raw: RawAPIDict) -> bool:
    """True iff GitLab reports the open MR as a hard merge conflict.

    Reads the three signals the MR list payload exposes: ``has_conflicts``
    (the authoritative boolean), the deprecated ``merge_status ==
    cannot_be_merged``, and ``detailed_merge_status == conflict``. An
    ``unchecked`` / ``can_be_merged`` / empty status is never a conflict — a
    still-computing mergeability state is left for a later sweep rather than
    raising a false alarm.
    """
    if raw.get("has_conflicts") is True:
        return True
    if raw.get("merge_status") == _MERGE_STATUS_CONFLICT:
        return True
    return raw.get("detailed_merge_status") == _DETAILED_MERGE_STATUS_CONFLICT


def collect_conflicted_mrs(raw_prs: list[RawAPIDict], result: SyncResult) -> None:
    """Append every conflicted open authored MR in *raw_prs* to *result*."""
    for raw in raw_prs:
        if not is_conflicted(raw):
            continue
        repo_path = extract_repo_path(raw)
        result.conflicted_mrs.append(
            ConflictedMR(
                iid=int(cast("SupportsInt", raw.get("iid", 0))),
                repo=repo_path.rsplit("/", maxsplit=1)[-1],
                web_url=str(raw.get("web_url", "")),
                title=str(raw.get("title", "")),
            ),
        )
