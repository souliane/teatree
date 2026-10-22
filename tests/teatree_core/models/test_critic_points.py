from teatree.core.models.critic_points import CriticPoint


def test_point_round_trip_preserves_location_and_kind() -> None:
    point = CriticPoint.coerce(
        {"kind": "nit", "summary": "rename helper", "citation": "x.py:2", "file": "x.py", "line": 2}
    )

    assert point.as_dict() == {
        "kind": "nit",
        "summary": "rename helper",
        "citation": "x.py:2",
        "file": "x.py",
        "line": 2,
    }


def test_unknown_kind_or_blank_summary_fails_closed() -> None:
    assert CriticPoint.coerce({"kind": "unknown", "summary": "suspect"}).kind == "blocking"
    assert CriticPoint.coerce({"kind": "nit", "summary": ""}).kind == "blocking"
    assert CriticPoint.coerce({"kind": "nit", "summary": "name", "line": -1}).line == 0
