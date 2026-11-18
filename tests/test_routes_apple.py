"""The Apple routes (macOS): the widened ``sockaddr_un`` on its own and
through the symlink, every route forced and checked, and what happens when
the kernel refuses the widened form."""

from __future__ import annotations

import errno
import os
import socket
import sys

import pytest
from _support import (
    TmpDirs,
    accept_ping,
    assert_empty,
    assert_far_from_cwd,
    assert_untouched,
    check_full_backlog,
    check_route,
    check_unreachable,
    forced,
    is_socket,
    mark_untouched,
    nest,
    posix_cfg,
    raises_errno,
    stale_socket,
    symlink_route,
    symlink_wide_route,
    with_name,
)

from sockat import _route
from sockat._route import BIND, CONNECT, CONNECT_NONBLOCKING, Route
from sockat._sys import DIRECT_MAX, SUN_PATH_CAP, WIDE_PATH_MAX, Op
from sockat._sys import op as real_op

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the widened sockaddr is Apple's")


def long_place(tmp: TmpDirs, total: int) -> bytes:
    d = nest(tmp.new(), total, b"place")
    os.mkdir(d)
    return d


# XNU takes a socket address of up to SOCK_MAXADDRLEN (255) bytes, so the
# widened form carries 255 - 2 = 253 bytes of path. Pinned against the running
# kernel: a path of 253 bytes binds and connects with the socket at the path; a
# path of 254 is refused (ENAMETOOLONG) before the kernel is asked.
def test_the_widened_sockaddr_carries_253_bytes(tmp: TmpDirs) -> None:
    assert WIDE_PATH_MAX == 253
    p = nest(tmp.new(), 253)
    with _route.wide_op(BIND, p) as listener:
        assert is_socket(p)
        with _route.wide_op(CONNECT, p) as client:
            accept_ping(listener, client)
    q = nest(tmp.new(), 254)
    with raises_errno(errno.ENAMETOOLONG):
        _route.wide_op(BIND, q)
    assert not is_socket(q)


# What the socket module reports for a socket bound through the widened form:
# XNU's getsockname copies at most the caller's buffer (the module passes a
# plain sockaddr_un, so the 104 bytes of sun_path) and the module reads the
# path up to the first NUL, of which the kernel wrote none. So the address
# shown starts with the first 104 bytes of the path, cut short there (the
# SPEC's "a path cut short"), followed by whatever bytes the module finds
# after its buffer before a NUL (seen: none, or a few bytes of garbage; a
# CPython behaviour, not sockat's). It must not raise, and it is not the path:
# users keep their own. The client's peer address and the accepted stream's
# addresses must at least be readable.
def test_the_address_of_a_widened_bind_is_cut_short(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 200)
    with _route.wide_op(BIND, p) as listener:
        shown = os.fsencode(listener.getsockname())
        assert shown != p
        assert shown.startswith(p[:SUN_PATH_CAP]), shown
        with _route.wide_op(CONNECT, p) as client:
            client.getpeername()
            server, _ = listener.accept()
            with server:
                server.getsockname()
                server.getpeername()


# Every Apple route but the relative ones (forced in a child process in
# test_routes_posix) gets every check of a route (see check_route: the socket
# at the path, data both ways, blocking / nonblocking connects, close-on-exec,
# the peer is this process, the cwd unchanged, nothing left in the place) and
# the nonblocking connect against a full backlog (on macOS a full backlog
# fails at once with ECONNREFUSED for a blocking connect too, so what differs
# there is the socket that comes back: it must be nonblocking on every route):
#
# * the path itself (DIRECT_MAX bytes), through the socket module;
# * the widened sockaddr with the path itself: over DIRECT_MAX bytes (and no
#   relative spelling within DIRECT_MAX: under /tmp, far from the cwd) and at
#   most 253, for DIRECT_MAX + 1, 200 and 253 bytes;
# * the symlink under a short place (the plain sockaddr) and under a 150-byte
#   place (the spelling through it needs the widened sockaddr: the
#   SYMLINK_*_WIDE routes), a path of 600 bytes;
# * the thread-cwd route (600 bytes, no place), in test_routes_posix.
@pytest.mark.parametrize("what", ["direct", "wide-104", "wide-200", "wide-253", "symlink", "symlink-wide"])
def test_every_apple_route(tmp: TmpDirs, what: str) -> None:
    t = tmp.new()
    place = tmp.new()
    if what == "direct":
        f = forced(what, nest(t, DIRECT_MAX), None, Route.DIRECT)
    elif what.startswith("wide-"):
        total = int(what[5:])
        f = forced(what, nest(t, total), posix_cfg(), Route.WIDE)
    elif what == "symlink":
        f = forced(what, nest(t, 600), posix_cfg([place]), symlink_route)
    else:
        place = long_place(tmp, 150)
        f = forced(what, nest(t, 600), posix_cfg([place]), symlink_wide_route)
    if f.want(BIND) is not Route.DIRECT:
        assert_far_from_cwd(f.path)
    check_route(f, [place])
    check_full_backlog(f)
    assert_empty(place)


# A failure on the widened route is the kernel's error for the path (ENOENT
# for a missing directory and for a missing socket, EADDRINUSE for an existing
# one, ECONNREFUSED for a socket nobody listens on): EINVAL / ENAMETOOLONG are
# what XNU answers for an address it will not take, these are not, so none of
# them is taken as "the kernel refuses the widened form". Nothing after the
# route is tried: the place for the symlink stays untouched (the thread-cwd
# route is on too).
def test_a_failure_on_the_widened_route_is_not_retried(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 200)
    place = tmp.new()
    c = posix_cfg([place], thread_cwd=True)
    stale = with_name(p, b"stale.sock")
    stale_socket(stale)
    mark_untouched(place)
    for req in (BIND, CONNECT):
        with pytest.raises(FileNotFoundError):
            _route.run_long(req, with_name(p, b"nope/s.sock"), c)
    with pytest.raises(FileNotFoundError):
        _route.run_long(CONNECT, p, c)
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is Route.WIDE
        with raises_errno(errno.EADDRINUSE):
            _route.run_long(BIND, p, c)
    for req in (CONNECT, CONNECT_NONBLOCKING):
        with pytest.raises(ConnectionRefusedError):
            _route.run_long(req, stale, c)
    assert_untouched(place)
    assert not _route.refuses_wide(OSError(errno.ENOENT, "x"))
    assert _route.refuses_wide(OSError(errno.EINVAL, "x"))
    assert _route.refuses_wide(OSError(errno.ENAMETOOLONG, "x"))


def refuse_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for a kernel that refuses the widened sockaddr: every
    bind(2) / connect(2) with a widened address (no NUL at the end) fails
    with EINVAL, as XNU answers an address it will not take; standard
    addresses go through."""

    def op(which: Op, sock: socket.socket, addr: bytes) -> None:
        if not addr.endswith(b"\0"):
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
        real_op(which, sock, addr)

    monkeypatch.setattr("sockat._route.op", op)


# A kernel that refuses the widened form (EINVAL) makes the widened route
# unusable, not an error, and every later step judges lengths by the plain
# sockaddr:
#
# * a 200-byte path that would go in the widened form goes through the
#   symlink instead; a 150-byte place (whose spelling would need the widened
#   form) is passed over untouched for the short place after it;
# * a 600-byte path meets the refusal under the 150-byte place (the link there
#   is made, the widened call refused, the link removed) and goes on under the
#   short place in the plain sockaddr;
# * a file name of 150 bytes, which the thread-cwd route would carry in the
#   widened form, is then too long for the thread-cwd route, and the path
#   ends at the fork route's place (ENAMETOOLONG) with nothing created.
def test_a_kernel_refusing_the_widened_form_moves_on_in_the_plain_sockaddr(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    refuse_wide(monkeypatch)
    t = tmp.new()
    long150 = long_place(tmp, 150)
    short = tmp.new()
    c = posix_cfg([long150, short], thread_cwd=True)

    p = nest(t, 200, b"a.sock")
    mark_untouched(long150)
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is Route.SYMLINK_TO_DIR
        client, r = _route.run_long(CONNECT, p, c)
        with client:
            assert r is Route.SYMLINK_TO_FILE
            accept_ping(listener, client)
    assert_untouched(long150)

    q = nest(t, 600, b"b.sock")
    listener, r = _route.run_long(BIND, q, c)
    with listener:
        assert r is Route.SYMLINK_TO_DIR
        assert is_socket(q)
    assert_empty(long150)
    assert_empty(short)

    check_unreachable(nest(t, 600, b"n" * 150), posix_cfg(thread_cwd=True))
