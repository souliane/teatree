from teatree.core.models.types import validated_ticket_extra


def test_dream_release_and_funnel_survive_ticket_extra_validation() -> None:
    release = {"at": "2026-10-25T00:00:00+00:00", "reason": "branch absent", "batch": [{"gap_key": "g"}]}
    funnel = [{"at": "2026-10-25T00:00:00+00:00", "pending": 1}]

    extra = validated_ticket_extra({"dream_gap_released": release, "dream_funnel": funnel})

    assert extra["dream_gap_released"] == release
    assert extra["dream_funnel"] == funnel
