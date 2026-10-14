from dataclasses import replace

from teatree.core.evidence.forge_content import ContentVerdictKind, PrContentSnapshot, classify_content

BASE = {"src/a.py": "a0", "src/b.py": "b0", "src/c.py": "c0", "docs/old.md": "o0"}
HEAD = {"src/a.py": "a1", "src/b.py": "b1", "src/c.py": "c1", "src/new.py": "n1"}
CHANGED = ("docs/old.md", "src/a.py", "src/b.py", "src/c.py", "src/new.py")
UNRELATED = {"src/a.py": "a9", "src/b.py": "b9", "src/c.py": "c9", "docs/old.md": "o9"}

REWRITTEN = PrContentSnapshot(
    merged=True,
    merge_commit="m" * 40,
    content_head="h" * 40,
    merge_base="b" * 40,
    default_branch="main",
    default_tip="t" * 40,
    base_blobs=BASE,
    head_blobs=HEAD,
    merge_blobs=UNRELATED,
    tip_blobs=UNRELATED,
)


def test_content_found_nowhere_on_the_default_branch_is_absent() -> None:
    verdict = classify_content(REWRITTEN)

    assert verdict.kind is ContentVerdictKind.ABSENT
    assert verdict.absent == verdict.changed == CHANGED


def test_one_changed_path_present_at_the_tip_refuses() -> None:
    verdict = classify_content(replace(REWRITTEN, tip_blobs={**UNRELATED, "src/b.py": "b1"}))

    assert verdict.kind is ContentVerdictKind.PRESENT_AT_TIP
    assert "src/b.py" not in verdict.absent


def test_one_changed_path_carried_by_the_merge_commit_refuses() -> None:
    verdict = classify_content(replace(REWRITTEN, merge_blobs={**UNRELATED, "src/c.py": "c1"}))

    assert verdict.kind is ContentVerdictKind.CARRIED_BY_MERGE
    assert verdict.absent == tuple(path for path in CHANGED if path != "src/c.py")


def test_a_true_merge_whose_files_main_edited_later_is_carried() -> None:
    merge_commit = {**HEAD, "src/a.py": "a1+concurrent-main-edit"}
    verdict = classify_content(replace(REWRITTEN, merge_blobs=merge_commit, tip_blobs=UNRELATED))

    assert verdict.kind is ContentVerdictKind.CARRIED_BY_MERGE


def test_a_merge_commit_in_the_default_history_refuses_whatever_the_blobs_say() -> None:
    verdict = classify_content(replace(REWRITTEN, merge_commit_on_default=True))

    assert verdict.kind is ContentVerdictKind.CARRIED_BY_MERGE


def test_a_content_head_in_the_default_history_refuses() -> None:
    verdict = classify_content(replace(REWRITTEN, content_head_on_default=True))

    assert verdict.kind is ContentVerdictKind.PRESENT_AT_TIP


def test_a_deletion_the_tip_also_lacks_is_present() -> None:
    tip = {key: value for key, value in UNRELATED.items() if key != "docs/old.md"}
    verdict = classify_content(replace(REWRITTEN, tip_blobs=tip))

    assert verdict.kind is ContentVerdictKind.PRESENT_AT_TIP
    assert "docs/old.md" not in verdict.absent


def test_an_empty_diff_is_unknown_never_absent() -> None:
    verdict = classify_content(replace(REWRITTEN, head_blobs=BASE))

    assert verdict.kind is ContentVerdictKind.UNKNOWN


def test_an_unreadable_snapshot_is_unknown_with_its_reason() -> None:
    verdict = classify_content(PrContentSnapshot(unreadable="HTTP 429 on the tree read"))

    assert verdict.kind is ContentVerdictKind.UNKNOWN
    assert verdict.reason == "HTTP 429 on the tree read"


def test_an_unmerged_pr_has_no_merge_to_void() -> None:
    assert classify_content(PrContentSnapshot(merged=False)).kind is ContentVerdictKind.NOT_MERGED
