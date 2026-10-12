from teatree.core.forge_content import ContentPresence, ForgeTree, assess_content


def _assess(
    *, head_tree: str = "h", default_tree: str = "d", head: str | None = "a", default: str | None = "b"
) -> ContentPresence:
    return assess_content(
        head=ForgeTree("a" * 40, head_tree, {"src/a.py": head}),
        default=ForgeTree("b" * 40, default_tree, {"src/a.py": default}),
        changed_paths=["src/a.py"],
    ).presence


def test_different_changed_path_blobs_prove_absence() -> None:
    assert _assess() is ContentPresence.ABSENT


def test_matching_tree_or_blob_refuses_void() -> None:
    assert _assess(head_tree="same", default_tree="same") is ContentPresence.PRESENT
    assert _assess(head="same", default="same") is ContentPresence.PRESENT
    assert _assess(head=None, default=None) is ContentPresence.PRESENT


def test_incomplete_read_cannot_prove_absence() -> None:
    evidence = assess_content(
        head=ForgeTree("a" * 40, "h", {}),
        default=ForgeTree("b" * 40, "d", {"src/a.py": "b"}),
        changed_paths=["src/a.py"],
    )
    assert evidence.presence is ContentPresence.UNKNOWN
    assert (
        assess_content(
            head=ForgeTree("", "h", {"src/a.py": "a"}),
            default=ForgeTree("b" * 40, "d", {"src/a.py": "b"}),
            changed_paths=["src/a.py"],
        ).presence
        is ContentPresence.UNKNOWN
    )
