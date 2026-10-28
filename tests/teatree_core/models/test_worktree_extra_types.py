from teatree.core.models.types import WorktreeExtra, validated_worktree_extra


def test_worktree_extra_keeps_safe_checkout_audit() -> None:
    audit = {"digest": "verified", "process_evidence": "venue"}

    assert "auto_attestation" in WorktreeExtra.__annotations__
    assert validated_worktree_extra({"auto_attestation": audit})["auto_attestation"] == audit
