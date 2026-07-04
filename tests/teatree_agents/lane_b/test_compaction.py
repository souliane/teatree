from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from teatree.agents.lane_b.compaction import compact_history


def _msgs(n: int) -> list:
    out: list = []
    for i in range(n):
        if i % 2 == 0:
            out.append(ModelRequest(parts=[UserPromptPart(content=f"u{i}")]))
        else:
            out.append(ModelResponse(parts=[TextPart(content=f"a{i}")]))
    return out


class TestCompactHistory:
    def test_short_history_is_returned_unchanged(self) -> None:
        history = _msgs(5)
        assert compact_history(history, keep_recent=40) == history

    def test_long_history_keeps_first_plus_recent(self) -> None:
        history = _msgs(100)
        compacted = compact_history(history, keep_recent=10)
        assert len(compacted) == 11
        assert compacted[0] is history[0]
        assert compacted[1:] == history[-10:]

    def test_boundary_is_not_trimmed(self) -> None:
        history = _msgs(11)
        assert compact_history(history, keep_recent=10) == history

    def test_over_boundary_is_trimmed(self) -> None:
        history = _msgs(12)
        assert len(compact_history(history, keep_recent=10)) == 11

    def test_keep_recent_zero_is_a_noop(self) -> None:
        history = _msgs(50)
        assert compact_history(history, keep_recent=0) == history
