"""The one pattern that finds a closing keyword, shared by the merge and ship paths without the scanners."""

import re

# The colon form matches GitLab's ``Closes: #N``; ``Closes : #N`` (space before the colon) does not auto-close there.
CLOSE_KEYWORD_RE = re.compile(
    r"\b(?P<kw>close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?::\s*|\s+)"
    r"(?P<ref>(?:[\w./-]+)?#\d+|https?://\S+/issues/\d+)",
    re.IGNORECASE,
)
