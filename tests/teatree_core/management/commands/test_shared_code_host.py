"""The shared 'no code host configured' preamble helper (F3.7).

Eight commands used to open with divergent "no code host" wordings; this module
is the one canonical message + the ``{"error": ...}`` payload built from it.
"""

from teatree.core.management.commands._shared_code_host import NO_CODE_HOST_MESSAGE, no_code_host_error


def test_no_code_host_error_wraps_the_canonical_message() -> None:
    assert no_code_host_error() == {"error": NO_CODE_HOST_MESSAGE}


def test_canonical_message_names_the_actionable_fix() -> None:
    # The one message every preamble now shares — names both forge tokens so the
    # operator knows what to configure.
    assert "No code host configured" in NO_CODE_HOST_MESSAGE
    assert "GitLab/GitHub token" in NO_CODE_HOST_MESSAGE
