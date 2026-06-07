"""Unified-diff parsing primitives for inline review anchoring.

Split out of :mod:`teatree.cli.review` to keep that module under the
module-health LOC ceiling (`scripts/hooks/check_module_health.py`). This
module owns the *diff-side* of the inline-note workflow — the
``InlinePosition`` payload shape GitLab expects, and the helper that
locates a target line inside a unified-diff hunk so the caller can
refuse anchoring on a context/removed line (a defect surface for
inline-note posting before this guard existed).

No Django or GitLab-API dependency lives here on purpose: the helpers
are pure string/regex logic that can be exercised in unit tests
without spinning up the wider review machinery.
"""

import re
from typing import TypedDict, cast

from teatree.backends.gitlab.api import GitLabAPI

# GitLab change-entry dict in an MR /changes response. ``object`` rather
# than the actual narrow types because the API surface mixes strings
# (paths, diffs) and bools (renamed/new_file flags); a TypedDict would
# pin a fictitious schema. See ``teatree.backends.gitlab.api`` § ``RawMR``
# for the same pattern.
type ChangeEntry = dict[str, object]


class InlinePosition(TypedDict):
    """GitLab inline-note position payload (text diff anchoring)."""

    position_type: str
    base_sha: str
    head_sha: str
    start_sha: str
    old_path: str
    new_path: str
    new_line: int


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_NEARBY_LINE_RANGE = 5


def find_added_line(diff_text: str, target_line: int) -> tuple[bool, list[int]]:
    """Scan a unified-diff hunk text for ``target_line`` in the new file.

    Returns ``(is_added, nearby_added_lines)`` — ``is_added`` is True when the
    target line corresponds to an added (``+``) line in any hunk; the second
    element lists added line numbers within ±5 of the target for error hints.
    """
    is_added = False
    nearby: list[int] = []
    nl: int | None = None
    for line in diff_text.splitlines():
        m = _HUNK_HEADER.match(line)
        if m:
            nl = int(m.group(1))
            continue
        if nl is None:
            continue
        sign = line[:1] if line else " "
        if sign == "-":
            continue
        if sign == "+":
            if nl == target_line:
                is_added = True
            if abs(nl - target_line) <= _NEARBY_LINE_RANGE:
                nearby.append(nl)
        nl += 1
    return is_added, sorted(set(nearby))


def fetch_diff_refs(api: GitLabAPI, encoded_repo: str, mr: int) -> tuple[dict[str, str] | None, str]:
    """Return the MR's diff_refs (base/head/start SHAs) or an error message."""
    mr_data = api.get_json(f"projects/{encoded_repo}/merge_requests/{mr}")
    if not isinstance(mr_data, dict):
        return None, f"Could not fetch MR !{mr}"
    diff_refs_raw = mr_data.get("diff_refs", {})
    if not isinstance(diff_refs_raw, dict):
        return None, "MR has no diff_refs"
    return {str(k): str(v) for k, v in diff_refs_raw.items()}, ""


def fetch_file_diff(api: GitLabAPI, encoded_repo: str, mr: int, file: str) -> tuple[str | None, str]:
    """Return the raw unified diff for ``file`` in the MR, or an error message.

    Uses ``access_raw_diffs=true`` so large files collapsed by the default
    ``/diffs`` endpoint still surface their full hunks.
    """
    changes = api.get_json(f"projects/{encoded_repo}/merge_requests/{mr}/changes?access_raw_diffs=true")
    if not isinstance(changes, dict):
        return None, "Could not fetch MR changes to validate inline target"
    files_raw = changes.get("changes")
    if not isinstance(files_raw, list):
        return None, "MR changes response had no `changes` array"
    files = cast("list[ChangeEntry]", [f for f in files_raw if isinstance(f, dict)])
    match = next(
        (f for f in files if f.get("new_path") == file or f.get("old_path") == file),
        None,
    )
    if match is None:
        paths = [str(f.get("new_path")) for f in files]
        return None, f"File {file!r} is not changed in MR !{mr}. Changed files: {paths}"
    diff_text = str(match.get("diff") or "")
    if not diff_text:
        return None, (
            f"File {file!r} has no diff content in the MR API response (likely a collapsed large diff). "
            "draft_notes cannot anchor on collapsed files — use `t3 review post-comment` instead, "
            "or pick a smaller file."
        )
    return diff_text, ""


def resolve_inline_position(
    api: GitLabAPI,
    encoded_repo: str,
    mr: int,
    file: str,
    line: int,
) -> tuple[InlinePosition | None, str]:
    """Build a GitLab inline-note ``position`` dict, or return an error message.

    Validates that ``file:line`` is an added (``+``) line in the MR diff.
    """
    diff_refs, refs_error = fetch_diff_refs(api, encoded_repo, mr)
    if diff_refs is None:
        return None, refs_error
    diff_text, diff_error = fetch_file_diff(api, encoded_repo, mr, file)
    if diff_text is None:
        return None, diff_error
    is_added, nearby = find_added_line(diff_text, line)
    if not is_added:
        hint = f" Nearby added lines in this file: {nearby}." if nearby else ""
        return None, (
            f"Line {line} in {file} is not an added (`+`) line in the MR diff — "
            f"inline notes only anchor on added lines.{hint}"
        )
    position: InlinePosition = {
        "position_type": "text",
        "base_sha": diff_refs["base_sha"],
        "head_sha": diff_refs["head_sha"],
        "start_sha": diff_refs["start_sha"],
        "old_path": file,
        "new_path": file,
        "new_line": line,
    }
    return position, ""
