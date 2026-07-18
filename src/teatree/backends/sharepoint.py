"""Read-only SharePoint / OneDrive document-library backend via rclone (#3084).

Wraps the rclone ``onedrive`` backend pointed at a SharePoint document library
(``drive_type = documentLibrary``). The remote lives in an **encrypted**
``rclone.conf`` whose decryption password is unlocked non-interactively via
rclone's own ``RCLONE_PASSWORD_COMMAND`` (e.g. ``pass <entry>``) — the same
``pass`` pattern the Proton backup remote uses. The password is never written to
disk and never appears on the command line.

**Read-only is enforced at the OAuth-scope level**, not by a runtime flag: the
remote is configured with ``access_scopes = Files.Read Files.Read.All
Sites.Read.All offline_access`` (every ``*.ReadWrite`` dropped), so the issued
token physically cannot write and a write attempt returns HTTP 403. This client
reinforces that contract structurally — it only ever issues rclone READ
subcommands (``lsjson`` / ``cat`` / ``copyto`` from remote), and
:meth:`verify_read_only` asserts a write probe is refused.

The concrete tenant / site / remote / library values are client-specific and are
supplied from the environment (the ``TEATREE_SHAREPOINT_*`` wrapper vars resolved
in :func:`teatree.core.backend_factory.sharepoint_client_from_overlay`), never
committed to this repo.
"""

import json
import os
from typing import cast
from urllib.parse import quote

from teatree.types import ShareLinkVerification, SharePointEntry, SharePointRemoteSpec
from teatree.utils.run import CommandFailedError, CompletedProcess, run_checked

#: rclone subcommand that mutates the remote — used only by the read-only probe
#: in :meth:`SharePointClient.verify_read_only`, never on a data path.
_WRITE_PROBE_DIR = "__teatree_readonly_probe__"

_RCLONE_TIMEOUT_SECONDS = 120.0


class SharePointClient:
    """Read-only rclone client for a SharePoint / OneDrive document library.

    Configured from a :class:`~teatree.types.SharePointRemoteSpec`: ``remote`` is
    the rclone remote name (a trailing colon is added if absent); ``root`` is the
    library-relative path prefix every operation is resolved against;
    ``config_path`` points at the encrypted ``rclone.conf``; ``password_command``
    is the shell command rclone runs to obtain its decryption password
    (``pass <entry>``) — set as ``RCLONE_PASSWORD_COMMAND`` for the subprocess,
    empty to inherit whatever the ambient environment already provides;
    ``site_url`` and ``library_path`` back the ``?id=`` deep-link derivation.
    """

    def __init__(self, spec: SharePointRemoteSpec) -> None:
        self.remote = spec.remote if spec.remote.endswith(":") else f"{spec.remote}:"
        self.root = spec.root.strip("/")
        self.config_path = spec.config_path
        self.password_command = spec.password_command
        self.site_url = spec.site_url.rstrip("/")
        # The SharePoint server-relative path the ``?id=`` deep-link is built
        # from; defaults to ``root`` when the remote-relative and
        # server-relative library roots coincide.
        self.library_path = (spec.library_path or spec.root).strip("/")

    def list_files(self, subpath: str = "", *, recursive: bool = True) -> list[SharePointEntry]:
        """List entries under *subpath* as rclone ``lsjson`` records.

        Each record carries at least ``Path``, ``Name``, ``Size``, ``IsDir``
        and ``ModTime``. ``recursive`` walks the whole subtree.
        """
        cmd = ["lsjson", self._remote_path(subpath)]
        if recursive:
            cmd.append("--recursive")
        result = self._run(cmd)
        return cast("list[SharePointEntry]", json.loads(result.stdout or "[]"))

    def cat(self, file_path: str) -> str:
        """Stream one file's contents from the remote as text."""
        return self._run(["cat", self._remote_path(file_path)]).stdout

    def fetch(self, file_path: str, dest: str) -> str:
        """Copy one remote file to local *dest*, returning *dest*.

        ``copyto`` reads from the remote and writes only to the local
        filesystem — it never mutates the remote.
        """
        self._run(["copyto", self._remote_path(file_path), dest])
        return dest

    def share_link(self, folder_path: str = "") -> str:
        """Derive a stable ``?id=`` deep-link for a folder path.

        The link is the SharePoint ``onedrive.aspx`` viewer anchored at the
        library's server-relative path — stable per real folder, suitable for
        validating links pasted into outgoing documents.
        """
        server_relative = "/".join(part for part in (self.library_path, folder_path.strip("/")) if part)
        return f"{self.site_url}/_layouts/15/onedrive.aspx?id={quote('/' + server_relative)}"

    def verify_link(self, folder_path: str = "") -> ShareLinkVerification:
        """Verify *folder_path* exists on the remote and return its deep-link.

        ``exists`` is ``True`` only when the folder resolves on the live remote,
        so a link derived for a path that is not really there is caught before it
        ships.
        """
        return ShareLinkVerification(
            path=folder_path,
            url=self.share_link(folder_path),
            exists=self._path_exists(folder_path),
        )

    def verify_read_only(self) -> bool:
        """Assert the remote refuses writes; return ``True`` when it does.

        Issues an ``rclone mkdir`` probe. A refusal (rclone exits non-zero —
        the Graph 403 the read-only scopes force) is the expected, healthy
        outcome and returns ``True``. If the directory is created the remote is
        writable — the scope contract is broken — and this raises.
        """
        try:
            self._run(["mkdir", self._remote_path(_WRITE_PROBE_DIR)])
        except CommandFailedError:
            return True
        msg = (
            f"read-only contract violated: {self.remote} accepted a write "
            f"(mkdir {_WRITE_PROBE_DIR} succeeded) — the OAuth access_scopes are "
            f"not limited to Files.Read*/Sites.Read.All"
        )
        raise RuntimeError(msg)

    def _remote_path(self, subpath: str = "") -> str:
        joined = "/".join(part for part in (self.root, subpath.strip("/")) if part)
        return f"{self.remote}{joined}"

    def _path_exists(self, folder_path: str) -> bool:
        # ``lsjson`` exits 0 on a real folder (even an empty one) and non-zero on
        # a missing path — so a single probe is a faithful existence check.
        try:
            self.list_files(folder_path, recursive=False)
        except CommandFailedError:
            return False
        return True

    def _run(self, args: list[str]) -> "CompletedProcess[str]":
        """Run one rclone subcommand with the encrypted-config env, capturing output.

        Prepends ``rclone`` and ``--config <path>`` (when configured) and injects
        ``RCLONE_PASSWORD_COMMAND`` so rclone unlocks the encrypted config
        non-interactively. The password command rides the environment, never the
        argv.
        """
        cmd = ["rclone", *(("--config", self.config_path) if self.config_path else ()), *args]
        return run_checked(cmd, env=self._env(), timeout=_RCLONE_TIMEOUT_SECONDS)

    def _env(self) -> dict[str, str] | None:
        if not self.password_command:
            return None
        return {**os.environ, "RCLONE_PASSWORD_COMMAND": self.password_command}
