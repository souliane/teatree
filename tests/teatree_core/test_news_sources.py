"""The merged press-review source table — both source sets, deduped (#3669)."""

from teatree.core.news_sources import (
    EXISTING_SOURCES,
    NEWS_SOURCES,
    PRESS_REVIEW_SOURCES,
    NewsSource,
    merge_sources,
    normalize_source_url,
    render_source_directive,
)


class TestNormalizeSourceUrl:
    def test_strips_tracking_parameters(self) -> None:
        normalized = normalize_source_url("https://Example.com/post/?utm_source=x&id=7&fbclid=y")
        assert normalized == "https://example.com/post?id=7"

    def test_drops_the_fragment_and_trailing_slash(self) -> None:
        assert normalize_source_url("https://example.com/a/b/#top") == "https://example.com/a/b"

    def test_lowercases_scheme_and_host_only(self) -> None:
        assert normalize_source_url("HTTPS://Example.COM/Path") == "https://example.com/Path"

    def test_a_bare_root_keeps_its_slash(self) -> None:
        assert normalize_source_url("https://example.com/") == "https://example.com/"

    def test_a_non_url_passes_through_untouched(self) -> None:
        assert normalize_source_url("not a url") == "not a url"

    def test_two_spellings_of_one_feed_normalize_alike(self) -> None:
        left = normalize_source_url("https://tldr.tech/api/rss/ai/?utm_campaign=daily")
        right = normalize_source_url("https://TLDR.tech/api/rss/ai")
        assert left == right


class TestMergeSources:
    def test_keeps_both_source_sets(self) -> None:
        merged = merge_sources(EXISTING_SOURCES, PRESS_REVIEW_SOURCES)
        labels = {source.label for source in merged}
        assert {"TLDR AI", "The Rundown AI"} <= labels
        assert {"PyCoder's Weekly", "Django News", "Hacker News"} <= labels

    def test_an_overlapping_label_survives_once_with_the_existing_entry_winning(self) -> None:
        merged = merge_sources(EXISTING_SOURCES, PRESS_REVIEW_SOURCES)
        tldr = [source for source in merged if source.label == "TLDR AI"]
        assert len(tldr) == 1
        assert tldr[0].edition_dated is True

    def test_the_same_feed_under_two_labels_is_deduped_by_url(self) -> None:
        first = NewsSource(bucket="ai", label="One", url="https://example.com/feed", max_items=5)
        second = NewsSource(bucket="ai", label="Two", url="https://example.com/feed/?utm_source=x", max_items=5)
        assert [source.label for source in merge_sources([first], [second])] == ["One"]

    def test_the_shipped_table_is_the_merge(self) -> None:
        assert merge_sources(EXISTING_SOURCES, PRESS_REVIEW_SOURCES) == NEWS_SOURCES

    def test_every_shipped_source_declares_a_bucket_and_a_positive_item_cap(self) -> None:
        assert all(source.bucket and source.max_items > 0 for source in NEWS_SOURCES)


class TestRenderSourceDirective:
    def test_names_every_source_so_the_agent_fetches_the_merged_set(self) -> None:
        directive = render_source_directive(NEWS_SOURCES)
        assert all(source.label in directive for source in NEWS_SOURCES)

    def test_marks_the_edition_dated_sources_the_date_gate_applies_to(self) -> None:
        directive = render_source_directive(EXISTING_SOURCES)
        assert "edition-dated" in directive
