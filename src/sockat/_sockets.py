"""The socket type sockat returns for a long path (hub SPEC: an implementation
whose standard type lets the address be reported differently reports the
path given; docs/decisions/DR-0003).

For a path longer than ``sun_path``, the kernel reports the spelling the bind
went through (through B, D and E: ``/proc/<pid>/fd/<fd>/D/E/<name>``,
``<B>/D/E/<name>``, ``<B>/D/E``, or ``E/<name>`` with E removed already),
not the path. Code that reads the address back (asyncio's
``start_unix_server`` removes the socket file it names when the server
closes; ``repr``, logging) then works on another file than the path's, or
fails.
:class:`PathSocket` is a :class:`socket.socket` that reports the path given
instead.
"""

from __future__ import annotations

import socket
from typing import Any


class PathSocket(socket.socket):
    """A :class:`socket.socket` (``isinstance`` holds) whose address names
    the path given to sockat rather than the spelling the kernel was handed.

    * a listener (and every socket it accepts) reports the path as its
      :meth:`getsockname`;
    * a connected stream reports the path as its :meth:`getpeername`.

    The path is reported as a ``str`` decoded with :func:`os.fsdecode`, as
    the ``socket`` module reports a pathname address. Everything else is the
    standard socket's.
    """

    _sockat_name: str | None = None
    """What :meth:`getsockname` reports, or ``None`` for the kernel's."""
    _sockat_peer: str | None = None
    """What :meth:`getpeername` reports, or ``None`` for the kernel's."""

    def report_path(self, path: str | None, *, listening: bool) -> None:
        """Report ``path`` as this listener's own address (``listening``) or
        as this socket's peer's (``None``: the kernel's again)."""
        if listening:
            self._sockat_name = path
        else:
            self._sockat_peer = path

    # The standard socket's addresses are typed Any (typeshed's _RetAddress);
    # the overrides keep its signatures.
    #
    # The kernel is asked first even when the path is reported instead, so
    # that where the standard socket fails the call (closed: EBADF; a
    # listener's peer: ENOTCONN; a stream whose peer has gone, on macOS:
    # EINVAL) this one fails it the same way, rather than reporting a path
    # for a socket that has none any more.
    def getsockname(self) -> Any:  # noqa: ANN401
        kernel = super().getsockname()
        return kernel if self._sockat_name is None else self._sockat_name

    def getpeername(self) -> Any:  # noqa: ANN401
        kernel = super().getpeername()
        return kernel if self._sockat_peer is None else self._sockat_peer

    def accept(self) -> tuple[socket.socket, Any]:
        """Accept as :meth:`socket.socket.accept` does, returning a
        :class:`PathSocket` that reports the listener's path as its own
        address (its peer's is the kernel's, as for the standard socket).

        The standard ``accept`` builds a plain :class:`socket.socket`; its fd
        is taken over by a :class:`PathSocket` built the same way (same
        family, type, protocol, and the same blocking rule)."""
        conn, addr = super().accept()
        sock = PathSocket(conn.family, conn.type, conn.proto, fileno=conn.detach())
        # The same rule as the standard accept (bpo-7995): with no default
        # timeout, a listener with a timeout gives a blocking socket.
        if socket.getdefaulttimeout() is None and self.gettimeout():
            sock.setblocking(True)
        sock._sockat_name = self._sockat_name
        return sock, addr

    def dup(self) -> PathSocket:
        sock = super().dup()
        sock._sockat_name = self._sockat_name
        sock._sockat_peer = self._sockat_peer
        return sock
