import json
import os
import subprocess
import sys

_PROBE = """
import io, json
from wsgiref.util import setup_testing_defaults
from teatree.webhook_wsgi import application

def status(method, path):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "wsgi.input": io.BytesIO(b"")}
    setup_testing_defaults(environ)
    seen = []
    application(environ, lambda line, headers, exc_info=None: seen.append(line))
    return int(seen[0].split()[0])

def public_host_status():
    environ = {
        "REQUEST_METHOD": "GET", "PATH_INFO": "/hooks/github/", "HTTP_HOST": "forge.example.test",
        "wsgi.input": io.BytesIO(b""),
    }
    setup_testing_defaults(environ)
    seen = []
    application(environ, lambda line, headers, exc_info=None: seen.append(line))
    return int(seen[0].split()[0])

print(json.dumps({path: status(method, path) for method, path in (
    ("GET", "/admin/"), ("GET", "/dash/"), ("POST", "/hooks/github/"), ("POST", "/hooks/gitlab/"),
)}))
print(public_host_status())
"""


def test_listener_serves_no_admin() -> None:
    env = os.environ | {"DJANGO_SETTINGS_MODULE": "teatree.settings"}
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], check=False, capture_output=True, text=True, env=env, timeout=60
    )
    assert result.returncode == 0, result.stderr
    statuses = json.loads(result.stdout.splitlines()[0])

    assert statuses["/admin/"] == 404
    assert statuses["/dash/"] == 404
    assert statuses["/hooks/github/"] != 404
    assert statuses["/hooks/gitlab/"] != 404


def test_listener_accepts_public_host() -> None:
    env = os.environ | {"DJANGO_SETTINGS_MODULE": "teatree.settings"}
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], check=False, capture_output=True, text=True, env=env, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert int(result.stdout.splitlines()[1]) == 405
