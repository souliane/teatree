"""Where this checkout's GitLab pipeline lives.

Upstream keeps `.gitlab-ci.yml` beside the rest of core, so the repo root is the
answer and always has been. A HOST PROJECT that vendors core under
`vendor/teatree/` has one pipeline, not two — a second `.gitlab-ci.yml` inside the
vendored tree is never read by GitLab, which only ever loads the one at the
project root — so the host merges core's lanes into its own file and carries none
here.

Resolving the path instead of hardcoding it keeps one set of tests honest about
both layouts. Hardcoding the vendored spelling makes them fail on a
`FileNotFoundError` in a host project, which says nothing about the pipeline and
everything about the test; hardcoding the host spelling breaks them upstream,
where there is no parent project to walk up to.

The local file wins when it exists, so upstream's behaviour is untouched and only
the vendored-with-no-local-copy case walks up.
"""

from pathlib import Path

_CORE_ROOT = Path(__file__).resolve().parents[1]


def gitlab_ci_path() -> Path:
    """The `.gitlab-ci.yml` GitLab would actually load for this checkout."""
    local = _CORE_ROOT / ".gitlab-ci.yml"
    if local.exists():
        return local
    if _CORE_ROOT.name == "teatree" and _CORE_ROOT.parent.name == "vendor":
        return _CORE_ROOT.parents[1] / ".gitlab-ci.yml"
    return local
