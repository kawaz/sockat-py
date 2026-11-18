"""The socket type sockat returns: the standard :class:`socket.socket` for a
path the ``socket`` module takes, and for a longer one :class:`PathSocket`, a
subclass that reports the path given as the address (the listener's and every
stream it accepts as their own, a connected stream's as its peer) where the
kernel holds the spelling a route handed it. Everything else, the sockets it
makes (``accept``, ``dup``) and the errors included, is the standard's."""

from __future__ import annotations

import errno
import fcntl
import os
import socket
from collections.abc import Callable

import pytest
from _support import TmpDirs, accept_ping, kernel_peername, needs_long_bind, nest, ping

import sockat
from sockat._sockets import PathSocket
from sockat._sys import DIRECT_MAX

pytestmark = needs_long_bind


def fd_nonblocking(s: socket.socket) -> bool:
    """Whether the fd of ``s`` itself is ``O_NONBLOCK`` (which the socket
    object's own idea, ``getblocking``, need not match)."""
    return bool(fcntl.fcntl(s.fileno(), fcntl.F_GETFL) & os.O_NONBLOCK)


def blocking_state(s: socket.socket) -> tuple[float | None, bool, bool]:
    return s.gettimeout(), s.getblocking(), fd_nonblocking(s)


# The boundary of the type is the boundary of the routes: a path of DIRECT_MAX
# bytes goes to the socket module and every socket of it (the listener, both
# connects, an accepted stream, a dup) is a plain socket.socket with nothing
# added; one byte more takes a route and every socket of it is a PathSocket,
# which is a socket.socket (isinstance holds, so code that checks for the
# standard type takes it).
@pytest.mark.parametrize("total", [DIRECT_MAX, DIRECT_MAX + 1])
def test_the_type_is_the_standards_up_to_direct_max_and_a_path_socket_past_it(tmp: TmpDirs, total: int) -> None:
    want = socket.socket if total <= DIRECT_MAX else PathSocket
    p = nest(tmp.new(), total)
    with sockat.bind(p) as listener, listener.dup() as listener_dup:
        made: list[socket.socket] = [listener, listener_dup]
        for connect in (sockat.connect, sockat.connect_nonblocking):
            with connect(p) as client, client.dup() as client_dup:
                accepted, _ = listener.accept()
                with accepted, accepted.dup() as accepted_dup:
                    for s in (client, client_dup, accepted, accepted_dup):
                        assert type(s) is want
                    made += [client, client_dup, accepted, accepted_dup]
                    ping(accepted, client)
        for s in made:
            assert isinstance(s, socket.socket)
            assert type(s) is want


# accept on a listener of a long path returns a PathSocket that reports the
# listener's path as its own address (as the standard's accepted stream
# reports the path the listener was bound at), and the kernel's peer address
# as its peer, which for a client (not bound) is what the standard reports for
# a short path: the empty address. The accepted stream works.
def test_accept_returns_a_path_socket_that_reports_the_listeners_path(tmp: TmpDirs) -> None:
    t = tmp.new()
    short = os.fsdecode(t + b"/std.sock")
    p = nest(t, 300)
    with sockat.bind(short) as std, sockat.connect(short):
        std_accepted, _ = std.accept()
        with std_accepted:
            std_peer = std_accepted.getpeername()
    with sockat.bind(p) as listener, sockat.connect(p) as client:
        accepted, addr = listener.accept()
        with accepted:
            assert type(accepted) is PathSocket
            assert accepted.getsockname() == os.fsdecode(p)
            assert accepted.getpeername() == kernel_peername(accepted) == std_peer
            assert addr == std_peer
            ping(accepted, client)


# The blocking state of an accepted stream follows the standard accept, which
# PathSocket.accept builds its socket the way of: with no default timeout
# (socket.getdefaulttimeout() is None) a listener with a timeout gives a
# blocking stream (bpo-7995: some systems hand the listener's O_NONBLOCK on to
# the accepted fd, and the standard forces it back), and otherwise the stream
# is whatever the standard makes of the fd (macOS: from a nonblocking listener
# the fd stays O_NONBLOCK while the socket reports blocking; with a default
# timeout, the stream takes it). Every combination of the default timeout
# (none, 2.5 seconds) and the listener (blocking, a timeout, nonblocking) is
# compared with the standard's accept at a short path in the same state:
# gettimeout, getblocking and the fd's own O_NONBLOCK. The default timeout is
# process-wide, so it is put back to none afterwards.
@pytest.mark.parametrize("default", [None, 2.5])
@pytest.mark.parametrize("mode", ["blocking", "timeout", "nonblocking"])
def test_accept_follows_the_standards_blocking_rule(tmp: TmpDirs, default: float | None, mode: str) -> None:
    assert socket.getdefaulttimeout() is None, "the premise: no default timeout outside this test"
    t = tmp.new()
    short = os.fsdecode(t + b"/std.sock")
    p = nest(t, 300)
    set_mode: dict[str, Callable[[socket.socket], None]] = {
        "blocking": lambda s: s.settimeout(None),
        "timeout": lambda s: s.settimeout(1.5),
        "nonblocking": lambda s: s.setblocking(False),
    }
    with sockat.bind(short) as std, sockat.bind(p) as listener:
        for s in (std, listener):
            set_mode[mode](s)
        try:
            socket.setdefaulttimeout(default)
            with sockat.connect(short) as std_client, sockat.connect(p) as client:
                std_accepted, _ = std.accept()
                accepted, _ = listener.accept()
                with std_accepted, accepted:
                    assert type(accepted) is PathSocket
                    assert blocking_state(accepted) == blocking_state(std_accepted), (default, mode)
                    if default is None and mode == "timeout":
                        assert accepted.gettimeout() is None
                        assert not fd_nonblocking(accepted)
                    if default is not None:
                        assert accepted.gettimeout() == default
                    ping(std_accepted, std_client)
                    ping(accepted, client)
        finally:
            socket.setdefaulttimeout(None)


# dup gives the same kind of socket reporting the same address: a listener's
# dup reports the path and accepts streams that report it too, a client's dup
# reports the path as its peer, an accepted stream's dup as its own address.
# The timeout is copied as the standard's dup copies it (here a nonblocking
# client, 0.0).
def test_a_dup_reports_the_same_path(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 300)
    name = os.fsdecode(p)
    with sockat.bind(p) as listener, listener.dup() as listener_dup:
        assert type(listener_dup) is PathSocket
        assert listener_dup.getsockname() == name
        assert listener_dup.gettimeout() == listener.gettimeout()
        with sockat.connect(p) as other:
            accepted, _ = listener_dup.accept()
            with accepted:
                assert type(accepted) is PathSocket
                assert accepted.getsockname() == name
                ping(accepted, other)
        with sockat.connect_nonblocking(p) as client, client.dup() as client_dup:
            assert type(client_dup) is PathSocket
            assert client_dup.getpeername() == name
            assert client_dup.gettimeout() == client.gettimeout() == 0.0
            accepted, _ = listener.accept()
            with accepted, accepted.dup() as accepted_dup:
                assert type(accepted_dup) is PathSocket
                assert accepted_dup.getsockname() == name
                ping(accepted_dup, client_dup)


# repr shows the path (repr reads getsockname / getpeername), as the
# standard's repr shows a short path: `laddr=<path>` for the listener and an
# accepted stream, `raddr=<path>` for a client. Logging a socket names the
# socket file, not a /proc fd or a temporary symlink.
def test_the_repr_shows_the_path(tmp: TmpDirs) -> None:
    t = tmp.new()
    short = os.fsdecode(t + b"/std.sock")
    p = nest(t, 300)
    name = os.fsdecode(p)
    with sockat.bind(short) as std, sockat.connect(short) as std_client:
        assert f"laddr={short}" in repr(std), "the premise: the standard's repr"
        assert f"raddr={short}" in repr(std_client), "the premise: the standard's repr"
    with sockat.bind(p) as listener, sockat.connect(p) as client:
        accepted, _ = listener.accept()
        with accepted:
            assert f"laddr={name}" in repr(listener)
            assert f"raddr={name}" in repr(client)
            assert f"laddr={name}" in repr(accepted)


def address_outcome(call: Callable[[], object], path: str) -> tuple[str, object]:
    """What ``call`` (a ``getsockname`` / ``getpeername``) gives: the error's
    errno, ``path``, or any other address."""
    try:
        got = call()
    except OSError as e:
        return "error", e.errno
    return ("the path", None) if got == path else ("another address", got)


# Where the standard's socket fails to report an address, a PathSocket fails
# the same way rather than reporting the path: it asks the kernel first. A
# socket that has no address any more must not look as if it still had one.
# Compared with the standard's at a short path, step by step:
#
# * a listener's peer (not connected: ENOTCONN);
# * a client's peer once the accepted stream is closed (macOS: EINVAL; Linux
#   still reports the address, and so does the PathSocket: the path);
# * every address of a closed socket (EBADF).
def test_where_the_standard_fails_to_report_an_address_so_does_a_path_socket(tmp: TmpDirs) -> None:
    t = tmp.new()
    short = os.fsdecode(t + b"/std.sock")
    p = nest(t, 300)
    long = os.fsdecode(p)
    seen: dict[str, list[tuple[str, object]]] = {}
    for path in (short, long):
        steps: list[tuple[str, object]] = []
        listener = sockat.bind(path)
        client = sockat.connect(path)
        steps.append(address_outcome(listener.getpeername, path))
        accept_ping(listener, client)
        steps.append(address_outcome(client.getpeername, path))
        listener.close()
        client.close()
        for s in (listener, client):
            steps.append(address_outcome(s.getsockname, path))
            steps.append(address_outcome(s.getpeername, path))
        seen[path] = steps
    assert seen[long] == seen[short]
    assert seen[short][0] == ("error", errno.ENOTCONN), seen[short]
    assert seen[short][-4:] == [("error", errno.EBADF)] * 4, seen[short]
