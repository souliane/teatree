from teatree.core.intake.nit_priority import is_nit_issue, nit_ticket_extra


def test_label_shapes_and_case_are_recognized() -> None:
    assert is_nit_issue({"labels": [{"name": "NIT"}]})
    assert is_nit_issue({"labels": ["nit", "t3-auto"]})
    assert not is_nit_issue({"labels": ["bug"]})


def test_ticket_stamp_preserves_existing_context() -> None:
    issue = {"labels": ["nit"]}
    assert nit_ticket_extra({"owner_decision": "yes"}, issue) == {
        "owner_decision": "yes",
        "nit_follow_up": True,
    }
    assert nit_ticket_extra({"owner_decision": "yes"}, {"labels": []}) == {"owner_decision": "yes"}
