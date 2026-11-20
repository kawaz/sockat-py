""":mod:`sockat.datagram`'s calls with what they went through (the sockat
SPEC's ``datagram::bind_ex`` / ``connect_ex``)."""

from __future__ import annotations

import socket

from .. import _calls
from .._routes import Call, StrOrBytesPath
from .._spelling import Trail
from .._sys import Op
from . import Bound, Options, Report, _opts, _telling

__all__ = ["bind", "connect"]


def bind(path: StrOrBytesPath, *, options: Options | None = None) -> Bound:
    """:func:`sockat.datagram.bind`, with what it went through and the
    socket file (as :func:`sockat.ex.bind`)."""
    trail = Trail()
    with _telling(trail):
        done, file = _calls.bind(path, type=socket.SOCK_DGRAM, opts=_opts(options), remember=True, trail=trail)
    assert file is not None
    return Bound(done.sock, file, done.spelling, done.route, done.base, tuple(trail.skipped))


def connect(sock: socket.socket, path: StrOrBytesPath, *, options: Options | None = None) -> Report:
    """:func:`sockat.datagram.connect`, with what it went through."""
    trail = Trail()
    with _telling(trail):
        _, route = _calls.connect(
            path, call=Call(Op.CONNECT, socket.SOCK_DGRAM, sock=sock), opts=_opts(options), trail=trail
        )
    return Report(route, tuple(trail.skipped))
