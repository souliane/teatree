import teatree.backends.types as _types


def test_typed_responses_importable() -> None:
    for name in (
        "PullRequestResponse",
        "PipelineResponse",
        "QualityCheckResponse",
        "NoteResponse",
        "UploadResponse",
        "IssueResponse",
        "ChatResponse",
    ):
        td = getattr(_types, name)
        assert issubclass(td, dict)
