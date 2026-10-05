"""The guard a cold-hook subprocess test prepends to its driver: no Django, no overlay, no process.

A hook subprocess that imports Django pays for every overlay package too, which can outlast Claude
Code's hook timeout on a loaded host. The guard writes ``FORBIDDEN <what>`` to stderr and refuses any
Django or overlay-package import, and any process start but a ``git`` read (the gates' cheap repo
probe), so a test asserts ``FORBIDDEN not in stderr``.
"""

FORBIDDEN = "FORBIDDEN"

COLD_GUARD = r"""
import os, shlex, sys
from importlib.metadata import entry_points
_OVERLAYS = entry_points(group="teatree.overlays")
_COLD_FORBIDDEN = ("django", *(ep.value.split(":")[0].rsplit(".", 1)[0] for ep in _OVERLAYS))
_PROGRAM_ARG = {"subprocess.Popen": 1, "os.system": 0, "os.posix_spawn": 0, "os.spawn": 1, "os.exec": 0}

class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if any(name == root or name.startswith(root + ".") for root in _COLD_FORBIDDEN):
            sys.stderr.write(f"FORBIDDEN import {name}\n")
            raise ImportError(name)

def _program(event, args):
    spec = args[_PROGRAM_ARG[event]] if event in _PROGRAM_ARG else ""
    if isinstance(spec, (str, bytes, os.PathLike)):
        argv = shlex.split(os.fsdecode(spec))
    else:
        argv = [os.fsdecode(part) for part in spec or ()]
    return os.path.basename(argv[0]) if argv else ""

def _refuse_processes(event, args):
    if (event in _PROGRAM_ARG or event in {"os.fork", "os.forkpty"}) and _program(event, args) != "git":
        sys.stderr.write(f"FORBIDDEN {event} {_program(event, args)}\n")
        raise PermissionError(event)

sys.meta_path.insert(0, _Blocker())
sys.addaudithook(_refuse_processes)
"""

#: Runs the hook script named by ``argv[1]`` as ``__main__`` under :data:`COLD_GUARD`.
COLD_SCRIPT_DRIVER = (
    COLD_GUARD
    + r"""
import runpy
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
)

#: :data:`COLD_SCRIPT_DRIVER` for ``argv[3]`` with the hook's one stdout write failing: ``argv[1]`` is the plugin
#: root, and ``argv[2]`` is ``overrun`` (the time budget runs out at that instant), ``broken-pipe``, or
#: ``unread-pipe`` (the real write goes to a pipe the harness has stopped reading, so only its flush fails).
FAILED_WRITE_DRIVER = (
    COLD_GUARD
    + r"""
import runpy, signal
sys.path.insert(0, sys.argv[1])
import hooks.scripts.additional_context as additional_context
_FAILURE = sys.argv[2]

def _failed_write(event, context):
    if _FAILURE == "overrun":
        os.kill(os.getpid(), signal.SIGALRM)
    raise BrokenPipeError(event)

if _FAILURE == "unread-pipe":
    _unread, _written = os.pipe()
    os.close(_unread)
    os.dup2(_written, sys.stdout.fileno())
else:
    additional_context.emit_additional_context = _failed_write
sys.argv = sys.argv[3:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
)
