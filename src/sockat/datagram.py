"""``SOCK_DGRAM`` Unix domain sockets at paths longer than ``sun_path``: the
calls of the standard library's ``SOCK_DGRAM`` sockets that take a path, and
:func:`unbound` that goes with them (the hub SPEC's ``datagram::``)::

    import sockat

    server = sockat.datagram.bind(long_path)
    client = sockat.datagram.unbound()
    sockat.datagram.connect(client, long_path)
    client.send(b"hello")

A long path is reached by the routes of the stream calls (a bind's
spelling stays usable, so replies to a bound sender reach it; a connect
leaves nothing behind).
"""

from __future__ import annotations

import socket

from . import _calls
from ._routes import Call, StrOrBytesPath
from ._sockets import PathSocket
from ._sys import Op

__all__ = ["bind", "connect", "unbound"]


def bind(path: StrOrBytesPath) -> socket.socket:
    """Create an ``AF_UNIX`` datagram socket and bind it to ``path``, for
    any length of ``path``. The socket file is not removed when the socket
    is closed."""
    return _calls.bind(path, type=socket.SOCK_DGRAM)[0].sock


def unbound() -> socket.socket:
    """An ``AF_UNIX`` datagram socket bound to nothing (to :func:`connect`
    and send from)."""
    return PathSocket(socket.AF_UNIX, socket.SOCK_DGRAM)


def connect(sock: socket.socket, path: StrOrBytesPath) -> None:
    """Connect the datagram socket ``sock`` to ``path`` (its sends go there),
    for any length of ``path``. Nothing is left behind."""
    _calls.connect(path, call=Call(Op.CONNECT, socket.SOCK_DGRAM, sock=sock))
