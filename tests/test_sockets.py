"""The socket type sockat returns (hub SPEC "標準と同じにすること",
"カーネルが返すアドレス"): the standard :class:`socket.socket` for a path the
``socket`` module takes, and for a longer one :class:`PathSocket`, a subclass
that reports the path given as the address, as the standard reports a
short one, where the kernel holds the spelling of a route."""

from __future__ import annotations

import errno
import fcntl
import os
import socket

from _support import TmpDirs, kernel_peername, kernel_sockname, long_path, nest, ping, raises_errno

import sockat
import sockat.ex
from sockat._sockets import PathSocket
from sockat._sys import DIRECT_MAX


def options(tmp: TmpDirs) -> sockat.ex.Options:
    return sockat.ex.Options(base=tmp.new() + b"/b")


# Every socket of a long path is a PathSocket (a socket.socket: isinstance
# holds): the listener, its dup, both connects, an accepted stream; each
# reports the path given (the listener's and an accepted stream's as their
# own address, a connected stream's as its peer's), where the kernel reports
# the route's spelling. Reading the addresses never fails.
def test_a_long_paths_sockets_report_the_path(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    bound = sockat.ex.bind(p, options=options(tmp))
    with bound.socket as listener, listener.dup() as listener_dup:
        assert type(listener) is PathSocket
        assert listener.getsockname() == listener_dup.getsockname() == os.fsdecode(p)
        assert kernel_sockname(listener) != os.fsdecode(p)
        for connect in (sockat.connect, sockat.connect_nonblocking):
            with connect(p) as client:
                accepted, _ = listener.accept()
                with accepted:
                    assert type(client) is PathSocket
                    assert type(accepted) is PathSocket
                    assert isinstance(client, socket.socket)
                    assert client.getpeername() == os.fsdecode(p)
                    assert accepted.getsockname() == os.fsdecode(p)
                    kernel_peername(client)
                    client.setblocking(True)
                    ping(accepted, client)
        bound.file.remove()


# A path the socket module takes gets a plain socket.socket, nothing added.
def test_a_short_paths_sockets_are_the_standards(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), DIRECT_MAX)
    with sockat.bind(p) as listener, sockat.connect(p) as client:
        accepted, _ = listener.accept()
        with accepted:
            for s in (listener, client, accepted):
                assert type(s) is socket.socket
    os.unlink(p)


# Where the standard socket fails to report an address (closed: EBADF; a
# listener's peer: ENOTCONN), a PathSocket fails the same way rather than
# report the path.
def test_failures_are_the_standards(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    bound = sockat.ex.bind(p, options=options(tmp))
    listener = bound.socket
    with raises_errno(errno.ENOTCONN):
        listener.getpeername()
    bound.file.remove()
    listener.close()
    with raises_errno(errno.EBADF):
        listener.getsockname()


# The sockets are set up as the standard's: close-on-exec, the default
# blocking mode (nonblocking only from connect_nonblocking).
def test_the_sockets_are_set_up_as_the_standards(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    bound = sockat.ex.bind(p, options=options(tmp))
    with bound.socket as listener, sockat.connect(p) as c, sockat.connect_nonblocking(p) as n:
        for s in (listener, c, n):
            assert fcntl.fcntl(s.fileno(), fcntl.F_GETFD) & fcntl.FD_CLOEXEC
            assert not os.get_inheritable(s.fileno())
        assert listener.getblocking()
        assert c.getblocking()
        assert not n.getblocking()
        bound.file.remove()
