"""Balanced-scan JSON extractors prefer content-bearing spans (#2861)."""

from teatree.loops.dream.json_scan import first_content_bearing_object, first_object_bearing_array


class TestFirstObjectBearingArray:
    def test_prefers_the_object_bearing_array_over_a_prose_scalar_array_before_it(self) -> None:
        raw = 'Considering rules [1, 2, 3] I produce: [{"rule": "r"}]'
        assert first_object_bearing_array(raw) == [{"rule": "r"}]

    def test_prefers_the_object_bearing_array_over_an_empty_array_before_it(self) -> None:
        raw = 'There is nothing here [] but actually: [{"rule": "r"}]'
        assert first_object_bearing_array(raw) == [{"rule": "r"}]

    def test_falls_back_to_the_first_decodable_list_when_none_carries_an_object(self) -> None:
        # An all-scalar reply has no cluster object; the fallback keeps the payload so the
        # caller still classifies it as all-entries-dropped rather than reading nothing.
        raw = "prose [1, 2, 3] and more [4, 5]"
        assert first_object_bearing_array(raw) == [1, 2, 3]

    def test_returns_none_when_no_balanced_array_decodes(self) -> None:
        assert first_object_bearing_array("no array here [unbalanced") is None

    def test_skips_a_bracketed_prose_span_that_is_not_valid_json(self) -> None:
        raw = 'see [ref #2663] then [{"rule": "r"}]'
        assert first_object_bearing_array(raw) == [{"rule": "r"}]

    def test_does_not_descend_into_a_nested_array_of_the_first_span(self) -> None:
        # The first top-level span is a list of lists (no top-level object); the scan must
        # not return the object-bearing inner array — it advances past the decoded span.
        raw = '[[1], [2]] then [{"rule": "r"}]'
        assert first_object_bearing_array(raw) == [{"rule": "r"}]


class TestFirstContentBearingObject:
    def test_prefers_the_non_empty_object_over_a_prose_empty_object_before_it(self) -> None:
        raw = 'There is nothing {} but here: {"scenario_name": "s"}'
        assert first_content_bearing_object(raw) == {"scenario_name": "s"}

    def test_returns_the_first_object_when_it_is_already_non_empty(self) -> None:
        raw = 'prose {"scenario_name": "s"} and a trailing {"other": 1}'
        assert first_content_bearing_object(raw) == {"scenario_name": "s"}

    def test_falls_back_to_the_empty_object_when_none_carries_a_key(self) -> None:
        assert first_content_bearing_object("only {} here") == {}

    def test_returns_none_when_no_balanced_object_decodes(self) -> None:
        assert first_content_bearing_object("no object {unbalanced") is None

    def test_does_not_return_a_nested_object_of_the_first_empty_span(self) -> None:
        raw = '{} then {"outer": {"inner": 1}}'
        assert first_content_bearing_object(raw) == {"outer": {"inner": 1}}
