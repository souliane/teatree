from teatree.core.models.types import WorktreeExtra as PublicWorktreeExtra
from teatree.core.models.types import validated_worktree_extra
from teatree.core.models.worktree_extra_types import WorktreeExtra


def test_worktree_extra_reexport_keeps_safe_checkout_audit() -> None:
    audit = {"digest": "verified", "process_evidence": "venue"}

    assert PublicWorktreeExtra is WorktreeExtra
    assert validated_worktree_extra({"auto_attestation": audit})["auto_attestation"] == audit
