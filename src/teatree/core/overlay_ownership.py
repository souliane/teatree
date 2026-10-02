"""Does a declared workspace repo OWN a forge slug?

Two tiers, and the split is the point: a proper ``owner/name`` declaration is
authoritative, while a bare relative directory token is only a tiebreaker. A raw
substring test conflates them, which is how ``acme/widget`` came to own
``acme/widget-extra`` and ``t3-company`` came to own any URL containing it.
"""


def full_slug_owns(repo_slug: str, url_slug: str) -> bool:
    """True when the proper ``owner/name`` ``repo_slug`` owns ``url_slug``.

    Segment/boundary-aware, not a raw substring: ``repo_slug`` must carry at
    least one ``/`` (a real ``owner/name`` slug) and its ``/``-delimited
    segments must align as a suffix of ``url_slug``. A bare relative token
    (``t3-company``, as ``_discover_workspace_repos()`` emits) is rejected
    here — it can never own a URL by its directory name, closing the #1120
    misclassification where ``"t3-company" in <full URL>`` was True.

    Examples (``repo_slug`` owns ``url_slug``?):

    - ``company-fork-org/t3-company`` owns ``company-fork-org/t3-company`` (exact).
    - ``subgroup/repo`` owns ``group/subgroup/repo`` (segment suffix).
    - ``t3-company`` does NOT own ``company-fork-org/t3-company`` (bare token).
    - ``acme/widget`` does NOT own ``acme/widget-extra`` (segment differs).

    Case-folded on both sides: a forge slug names one repo whatever its case,
    and a declaration whose case differs from the URL's must not lose its repo
    to some other overlay's namespace claim on the same folded key.
    """
    repo_key = repo_slug.strip().lower()
    url_key = url_slug.strip().lower()
    if "/" not in repo_key:
        return False
    if repo_key == url_key:
        return True
    return url_key.split("/")[-repo_key.count("/") - 1 :] == repo_key.split("/")


def bare_name_owns(repo_token: str, url_slug: str) -> bool:
    """True when a bare repo-name ``repo_token`` matches ``url_slug``'s name segment.

    The weak tiebreaker tier: a relative directory token (no ``/``) is
    matched only against the trailing repo-name segment of ``url_slug``, on a
    full-segment boundary. This preserves overlays that legitimately own a
    repo but only expose its bare relative path (the bundled ``t3-teatree``
    overlay, whose ``get_workspace_repos()`` returns ``["teatree"]``), without
    the raw-substring collisions of the pre-#1120 matcher. Case-folded on both
    sides for the same reason :func:`full_slug_owns` is.
    """
    token = repo_token.strip().lower()
    return "/" not in token and url_slug.strip().lower().rsplit("/", 1)[-1] == token
