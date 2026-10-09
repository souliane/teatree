"""GitLab inline-note anchoring: the ``position`` an MR discussion takes, built from one read of the MR's diff."""

from dataclasses import dataclass
from typing import TypedDict, cast

from teatree.backends.gitlab.api import GitLabAPI
from teatree.utils.unified_diff import added_lines

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


_NEARBY_LINE_RANGE = 5


def find_added_line(diff_text: str, target_line: int) -> tuple[bool, list[int]]:
    """Whether ``target_line`` is an added (``+``) line, and the added lines within ±5 of it for an error hint."""
    added = added_lines(diff_text)
    return target_line in added, sorted(line for line in added if abs(line - target_line) <= _NEARBY_LINE_RANGE)


def fetch_diff_refs(api: GitLabAPI, encoded_repo: str, mr: int) -> tuple[dict[str, str] | None, str]:
    """Return the MR's diff_refs (base/head/start SHAs) or an error message."""
    mr_data = api.get_json(f"projects/{encoded_repo}/merge_requests/{mr}")
    if not isinstance(mr_data, dict):
        return None, f"Could not fetch MR !{mr}"
    diff_refs_raw = mr_data.get("diff_refs", {})
    if not isinstance(diff_refs_raw, dict):
        return None, "MR has no diff_refs"
    return {str(k): str(v) for k, v in diff_refs_raw.items()}, ""


@dataclass(frozen=True, slots=True)
class MrDiff:
    """One read of an MR's diff refs and raw changes, enough to anchor every comment of a review."""

    mr: int
    diff_refs: dict[str, str]
    files: list[ChangeEntry]

    @classmethod
    def fetch(cls, api: GitLabAPI, encoded_repo: str, mr: int) -> tuple["MrDiff | None", str]:
        """Read the diff refs, then the changes with ``access_raw_diffs=true`` so collapsed files keep their hunks."""
        diff_refs, refs_error = fetch_diff_refs(api, encoded_repo, mr)
        if diff_refs is None:
            return None, refs_error
        changes = api.get_json(f"projects/{encoded_repo}/merge_requests/{mr}/changes?access_raw_diffs=true")
        if not isinstance(changes, dict):
            return None, "Could not fetch MR changes to validate inline target"
        files_raw = changes.get("changes")
        if not isinstance(files_raw, list):
            return None, "MR changes response had no `changes` array"
        files = cast("list[ChangeEntry]", [f for f in files_raw if isinstance(f, dict)])
        return cls(mr=mr, diff_refs=diff_refs, files=files), ""

    def file_diff(self, file: str) -> tuple[str | None, str]:
        match = next(
            (f for f in self.files if f.get("new_path") == file or f.get("old_path") == file),
            None,
        )
        if match is None:
            paths = [str(f.get("new_path")) for f in self.files]
            return None, f"File {file!r} is not changed in MR !{self.mr}. Changed files: {paths}"
        diff_text = str(match.get("diff") or "")
        if not diff_text:
            return None, (
                f"File {file!r} has no diff content in the MR API response (likely a collapsed large diff). "
                "draft_notes cannot anchor on collapsed files — use `t3 review post-comment` instead, "
                "or pick a smaller file."
            )
        return diff_text, ""

    def position(self, file: str, line: int) -> tuple[InlinePosition | None, str]:
        """The inline-note ``position`` for ``file:line``, refused unless it is an added (``+``) line."""
        diff_text, diff_error = self.file_diff(file)
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
            "base_sha": self.diff_refs["base_sha"],
            "head_sha": self.diff_refs["head_sha"],
            "start_sha": self.diff_refs["start_sha"],
            "old_path": file,
            "new_path": file,
            "new_line": line,
        }
        return position, ""


def resolve_inline_position(
    api: GitLabAPI,
    encoded_repo: str,
    mr: int,
    file: str,
    line: int,
) -> tuple[InlinePosition | None, str]:
    """Build a GitLab inline-note ``position`` dict, or return an error message."""
    diff, error = MrDiff.fetch(api, encoded_repo, mr)
    if diff is None:
        return None, error
    return diff.position(file, line)
