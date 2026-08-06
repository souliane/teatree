"""Resolving ``ROOT_URLCONF`` must not drag in the model-client SDK.

Django resolves the entire urlconf on the first request, and ``runserver
--skip-checks`` defers that past the socket bind — so anything the urlconf
reaches is paid by the first caller, after the port already accepts. A
dashboard view chain that reaches ``pydantic_ai`` costs that caller 671 module
imports (measured ~2.4 s), long enough for a readiness probe to time out.

The edge was ``dash.live`` -> ``loops.loop_table`` -> ``loop.job_identity`` ->
``loop.scanners`` -> ``askuserquestion_reply`` -> ``loop.inbound_reading``,
whose module-level ``run_one_shot`` import existed only to supply a default
argument. This pins that no such edge comes back.
"""

import json
import os
import subprocess
import sys

#: Model-client SDK roots. A dashboard render path has no business reaching one.
_SDK_ROOTS = ("pydantic_ai", "openai", "anthropic")

_PROBE = """
import json, sys
import django
django.setup()
from django.urls import get_resolver
get_resolver().url_patterns
json.dump(sorted({m.split(".")[0] for m in sys.modules}), sys.stdout)
"""


def test_urlconf_resolution_does_not_import_the_model_client_sdk() -> None:
    """A fresh interpreter that resolves the urlconf loads no SDK root."""
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "tests.django_settings"
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"urlconf probe failed:\n{proc.stderr}"
    loaded = set(json.loads(proc.stdout))
    assert loaded.isdisjoint(_SDK_ROOTS), (
        f"resolving ROOT_URLCONF imported the model-client SDK {sorted(loaded & set(_SDK_ROOTS))}. "
        "The first request pays that import after the socket already accepts. Defer the import to "
        "its call site (bind on first use) rather than at module scope."
    )
