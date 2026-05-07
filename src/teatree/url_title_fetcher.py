"""Fetch PR/issue titles from GitLab/GitHub to enrich prompts before trigger matching.

Used by the UserPromptSubmit hook so a skill's keyword triggers can match the
content of a linked PR/issue, not just the prompt text. Without this, pasting
a bare PR URL from a generic-looking repo does not load skills that only the
PR's *title* would identify (e.g. a domain-specific skill on a feature PR).

Set ``T3_HOOK_FETCH_TITLES=0`` to disable. Titles are cached at
``~/.cache/teatree/url-titles.json`` indefinitely (titles rarely change;
delete the file to invalidate).
"""

import concurrent.futures
import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail

CACHE_FILE = Path.home() / ".cache" / "teatree" / "url-titles.json"
PER_FETCH_TIMEOUT = 1.5
TOTAL_BUDGET = 4.0
MAX_URLS = 10
MAX_WORKERS = 5

_GITLAB_URL_RE = re.compile(
    r"https?://(?:[\w.-]*gitlab\.[\w.-]+)/([^\s#?]+?)/-/(merge_requests|issues|work_items)/(\d+)",
    re.IGNORECASE,
)
_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([^/\s]+/[^/\s]+)/(pull|issues)/(\d+)",
    re.IGNORECASE,
)


def _load_cache() -> dict[str, str]:
    if not CACHE_FILE.is_file():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def _run_json(cmd: list[str]) -> dict:
    try:
        result = run_allowed_to_fail(cmd, expected_codes=None, timeout=PER_FETCH_TIMEOUT)
    except (CommandFailedError, TimeoutExpired, OSError):
        return {}
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _fetch_gitlab(repo: str, kind: str, iid: str) -> str:
    if not shutil.which("glab"):
        return ""
    endpoint = "merge_requests" if kind == "merge_requests" else "issues"
    project = repo.replace("/", "%2F")
    return _run_json(["glab", "api", f"projects/{project}/{endpoint}/{iid}"]).get("title", "")


def _fetch_github(repo: str, kind: str, num: str) -> str:
    if not shutil.which("gh"):
        return ""
    cmd_kind = "pr" if kind == "pull" else "issue"
    return _run_json(["gh", cmd_kind, "view", num, "--repo", repo, "--json", "title"]).get("title", "")


def _extract_jobs(prompt: str) -> list[tuple[str, Callable[[], str]]]:
    """Return [(cache_key, fetcher_callable), ...] for URLs found in the prompt."""
    jobs: list[tuple[str, Callable[[], str]]] = []
    for match in _GITLAB_URL_RE.finditer(prompt):
        repo, kind, iid = match.group(1), match.group(2).lower(), match.group(3)
        if kind == "work_items":
            kind = "issues"
        cache_key = f"gitlab:{repo}:{kind}:{iid}"
        jobs.append((cache_key, lambda r=repo, k=kind, i=iid: _fetch_gitlab(r, k, i)))
    for match in _GITHUB_URL_RE.finditer(prompt):
        repo, kind, num = match.group(1), match.group(2).lower(), match.group(3)
        cache_key = f"github:{repo}:{kind}:{num}"
        jobs.append((cache_key, lambda r=repo, k=kind, n=num: _fetch_github(r, k, n)))
    return jobs[:MAX_URLS]


def fetch_titles(prompt: str) -> list[str]:
    """Return titles for every GitLab/GitHub URL in *prompt*. Cached + parallel.

    Returns ``[]`` when ``T3_HOOK_FETCH_TITLES=0``, when no URLs are found, or
    when every fetch fails. Failures are not cached so transient errors retry
    next time.
    """
    if os.environ.get("T3_HOOK_FETCH_TITLES", "1") == "0":
        return []
    jobs = _extract_jobs(prompt)
    if not jobs:
        return []

    cache = _load_cache()
    titles: list[str] = []
    todo: list[tuple[str, Callable[[], str]]] = []
    for cache_key, fetcher in jobs:
        cached = cache.get(cache_key)
        if cached:
            titles.append(cached)
        else:
            todo.append((cache_key, fetcher))

    if not todo:
        return titles

    cache_dirty = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(todo), MAX_WORKERS)) as pool:
        future_to_key = {pool.submit(fetcher): key for key, fetcher in todo}
        try:
            for fut in concurrent.futures.as_completed(future_to_key, timeout=TOTAL_BUDGET):
                title = fut.result()
                if title:
                    titles.append(title)
                    cache[future_to_key[fut]] = title
                    cache_dirty = True
        except concurrent.futures.TimeoutError:
            pass

    if cache_dirty:
        _save_cache(cache)
    return titles


def enrich_prompt(prompt: str) -> str:
    """Append fetched PR/issue titles to *prompt* so trigger keywords can match them."""
    titles = fetch_titles(prompt)
    if not titles:
        return prompt
    suffix = "\n".join(f"[linked title: {t}]" for t in titles)
    return f"{prompt}\n{suffix}"
