"""The host hop: what a container half resolves, and the wrapper on the host carries out.

Some acts are not core's to perform — the binaries, the credentials, the loopback a
browser reaches and the ports a fetch means all live on the operator's own machine. So
each leaf here owns only the resolving half: it writes a plan where ``deploy/t3`` reads
it, and the host wrapper is the single place the act happens.

``host_run`` is the generic hop for any overlay tool leaf (the CLI stages a runner plus
its plan; the wrapper executes it and its exit code becomes the command's).
``peer_forward`` is the one that hop generalises — what opening a peer's loopback
forward lands on, and the command that opens it. Imported by submodule path; no eager
re-export (mock.patch targets name the defining submodule).
"""
