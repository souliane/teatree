"""Action cards have stable, scoped identities and reject malformed payloads."""

from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker, read_action


def test_marker_qualifies_kind_target_and_binding() -> None:
    action = ActionRequest(kind=OwnerActionKind.VOID, target="17", binding="digest")
    marker = action_marker(action)
    assert marker.startswith("owner-action:void:")
    assert len(marker) <= 64
    assert marker == action_marker(action)
    assert marker != action_marker(ActionRequest(kind=OwnerActionKind.ESCAPE, target="17", binding="digest"))
    assert marker != action_marker(ActionRequest(kind=OwnerActionKind.VOID, target="18", binding="digest"))
    assert marker != action_marker(ActionRequest(kind=OwnerActionKind.VOID, target="17", binding="other"))
    assert marker != action_marker(ActionRequest(kind=OwnerActionKind.VOID, target="17", binding="digest", task_id=4))
    assert action_marker(ActionRequest(kind=OwnerActionKind.VOID, target="17", binding="digest", task_id=4)) != (
        action_marker(ActionRequest(kind=OwnerActionKind.VOID, target="17", binding="digest", task_id=5))
    )


def test_action_reader_refuses_malformed_payloads() -> None:
    assert read_action({"action": {"kind": "void", "target": "17", "binding": "digest"}}) is not None
    assert read_action({"action": {"kind": "attest", "target": "17", "binding": "digest"}}) is None
    assert read_action({"action": {"kind": "attest", "target": "", "binding": "digest"}}) is None
    assert read_action({"action": {"kind": "unknown", "target": "17", "binding": "digest"}}) is None
    assert read_action({"action": "attest"}) is None
    assert read_action({"action": {"kind": "attest", "target": "17", "binding": "digest", "task_id": True}}) is None
    assert read_action({"action": {"kind": "attest", "target": "17", "binding": "digest", "task_id": 0}}) is None
