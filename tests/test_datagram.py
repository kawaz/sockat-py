"""``sockat.datagram`` and ``sockat.ex.datagram``: a datagram socket bound at
a long path, an unbound one connected there, and the reply to a sender bound
at a long path (its spelling stays usable: DR-0017)."""

from __future__ import annotations

import os
import socket
import tempfile

import pytest
from _support import (
    HAS_PROC,
    THREAD_CWD,
    TmpDirs,
    env_at,
    kernel_sockname,
    long_path,
    raises_errno,
    run_child,
    standard_errno,
)

import sockat
import sockat.ex
import sockat.ex.datagram
from sockat import _calls
from sockat._routes import Call
from sockat._sys import IS_APPLE, Op


def dgram_listener_at(path: bytes) -> socket.socket:
    """A datagram socket at ``path`` that does not depend on the routes
    (bound under a short name, renamed)."""
    d = tempfile.mkdtemp(prefix="spt-d-", dir="/tmp")
    short = os.path.join(d, "d.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(short)
    os.rename(short, path)
    os.rmdir(d)
    return s


# A datagram socket bound at a long path receives what an unbound socket
# connected there sends; the bound socket reports the path as its address.
def test_bind_and_connect(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    with sockat.datagram.bind(p) as server, sockat.datagram.unbound() as client:
        assert server.type == socket.SOCK_DGRAM
        assert server.getsockname() == os.fsdecode(p)
        sockat.datagram.connect(client, p)
        client.send(b"hello")
        assert server.recv(16) == b"hello"
    os.unlink(p)


# The _ex calls: bind returns Bound (no listen on a datagram socket), connect
# a Report of the route and the routes skipped.
def test_ex(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    o = sockat.ex.Options(base=tmp.new() + b"/b")
    bound = sockat.ex.datagram.bind(p, options=o)
    with bound.socket as server, sockat.datagram.unbound() as client:
        assert isinstance(bound.spelling, sockat.ex.Linked)
        report = sockat.ex.datagram.connect(client, p)
        assert report.route != "standard"
        assert report.skipped[0].name == "standard"
        client.send(b"x")
        assert server.recv(1) == b"x"
        bound.file.remove()
    assert not os.path.lexists(p)


# A sender bound at a long path gets replies: the receiver sees the
# spelling the sender was bound through and sends back to it (the spelling
# names the sender's socket while it lives).
def test_a_reply_reaches_a_sender_bound_at_a_long_path(tmp: TmpDirs) -> None:
    rp = long_path(tmp.new(), name=b"r.sock")
    sp = long_path(tmp.new(), name=b"s.sock")
    with dgram_listener_at(rp) as receiver, sockat.datagram.bind(sp) as sender:
        sockat.datagram.connect(sender, rp)
        sender.send(b"ping")
        data, addr = receiver.recvfrom(16)
        assert data == b"ping"
        assert addr == kernel_sockname(sender)
        receiver.sendto(b"pong", addr)
        assert sender.recv(16) == b"pong"
    os.unlink(sp)


# A datagram connect goes by the routes of a stream connect, on the caller's
# socket, each forced: it reaches the receiver, and the socket is the
# caller's (not replaced).
@pytest.mark.parametrize("route", ["link-file", *(["thread-cwd-direct"] if THREAD_CWD else [])])
def test_each_route_on_the_callers_socket(tmp: TmpDirs, route: str) -> None:
    env = env_at(tmp.new())
    p = long_path(tmp.new())
    with dgram_listener_at(p) as receiver, sockat.datagram.unbound() as client:
        sock, got = _calls.connect(p, call=Call(Op.CONNECT, socket.SOCK_DGRAM, sock=client), env=env, order=(route,))
        assert sock is client
        assert got == route
        client.send(b"x")
        assert receiver.recv(1) == b"x"


# A failed route leaves the caller's socket open (it is the caller's to close).
def test_a_failure_leaves_the_callers_socket_open(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    with sockat.datagram.unbound() as client:
        with raises_errno(standard_errno("connect-missing", socket.SOCK_DGRAM)):
            sockat.datagram.connect(client, p)
        assert client.fileno() >= 0


# The caller's datagram socket connected to a long path reports it as its
# peer; connected again to a short path, it reports the kernel's again (not
# the long path any more).
def test_reconnecting_reports_the_new_peer(tmp: TmpDirs) -> None:
    long_p = long_path(tmp.new())
    short_p = tmp.new() + b"/s.sock"
    with dgram_listener_at(long_p), dgram_listener_at(short_p), sockat.datagram.unbound() as client:
        sockat.datagram.connect(client, long_p)
        assert client.getpeername() == os.fsdecode(long_p)
        sockat.datagram.connect(client, short_p)
        # The kernel's: the name the receiver was bound under (it was renamed
        # to the short path afterwards), not the long path.
        assert client.getpeername() == socket.socket.getpeername(client)
        assert client.getpeername() != os.fsdecode(long_p)
        sockat.datagram.connect(client, long_p)
        assert client.getpeername() == os.fsdecode(long_p)


# The routes that need no write (proc-direct, widened) work on the caller's
# datagram socket too.
NO_WRITE_ROUTES = [*(["proc-direct"] if HAS_PROC else []), *(["widened"] if IS_APPLE else [])]


@pytest.mark.skipif(not NO_WRITE_ROUTES, reason="neither /proc nor the widened sockaddr here")
@pytest.mark.parametrize("route", NO_WRITE_ROUTES or ["none"])
def test_no_write_routes_on_the_callers_socket(tmp: TmpDirs, route: str) -> None:
    env = env_at(tmp.new())
    p = long_path(tmp.new())
    with dgram_listener_at(p) as receiver, sockat.datagram.unbound() as client:
        sock, got = _calls.connect(p, call=Call(Op.CONNECT, socket.SOCK_DGRAM, sock=client), env=env, order=(route,))
        assert (sock, got) == (client, route)
        client.send(b"x")
        assert receiver.recv(1) == b"x"


# relative on the caller's datagram socket, from a cwd near the socket (in a
# child, so the test's cwd stays).
def test_relative_on_the_callers_socket(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    with dgram_listener_at(p) as receiver:
        out = run_child(
            """
            from _support import env_at
            from sockat import _calls, datagram
            from sockat._routes import Call
            from sockat._sys import IS_APPLE, Op
            client = datagram.unbound()
            env = env_at(b"/nonexistent")
            call = Call(Op.CONNECT, socket.SOCK_DGRAM, sock=client)
            _, route = _calls.connect(os.fsencode(ARGS["p"]), call=call, env=env, order=("relative",))
            client.send(b"x")
            report(route=route)
            """,
            cwd=os.path.dirname(p),
            args={"p": os.fsdecode(p)},
        )
        assert out["route"] == "relative"
        assert receiver.recv(1) == b"x"


# A datagram connect to a long path where a socket file nobody holds is
# fails with the OS's errno, the one the standard gets for a short path here
# (ECONNREFUSED on most systems, EDESTADDRREQ on illumos): sockat passes it
# through, and leaves the caller's socket open.
def test_a_connect_to_a_socket_nobody_holds(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    with dgram_listener_at(p):
        pass
    with sockat.datagram.unbound() as client:
        with raises_errno(standard_errno("connect-stale", socket.SOCK_DGRAM)):
            sockat.datagram.connect(client, p)
        assert client.fileno() >= 0
