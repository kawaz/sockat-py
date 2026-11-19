"""The Apple routes of a connect (macOS): the widened ``sockaddr_un`` on
its own and through the symlink, every route forced and checked, what
happens when the kernel refuses the widened form, and the bind, which never
takes the widened form (the routes through D spells it within ``sun_path``)."""

from __future__ import annotations

import errno
import os
import socket
import sys

import pytest
from _support import (
    UNTOUCHED,
    TmpDirs,
    accept_ping,
    assert_empty,
    assert_far_from_cwd,
    assert_untouched,
    check_full_backlog,
    check_route,
    d_cfg,
    fallback_cfg,
    forced,
    kernel_peername,
    kernel_sockname,
    listener_at,
    listing,
    mark_untouched,
    nest,
    parent,
    raises_errno,
    routes,
    stale_socket,
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
# kernel: a connect in the widened form to a path of 253 bytes reaches the
# listener there (bound by the routes through D); a path of 254 is refused
# (ENAMETOOLONG) before the kernel is asked.
def test_the_widened_sockaddr_carries_253_bytes(tmp: TmpDirs) -> None:
    assert WIDE_PATH_MAX == 253
    p = nest(tmp.new(), 253)
    listener, r = _route.run_long(BIND, p, d_cfg())
    with listener:
        assert r is Route.THREAD_CWD
        with _route.wide_op(CONNECT, p) as client:
            accept_ping(listener, client)
    q = nest(tmp.new(), 254)
    with raises_errno(errno.ENAMETOOLONG):
        _route.wide_op(CONNECT, q)


# Why no bind takes the widened form: the address of a socket bound in it
# (called here directly, not through a route) does not fit sun_path, and the
# socket module cannot report it. XNU's getsockname copies at most the
# caller's buffer (the module passes a plain sockaddr_un, so the 104 bytes of
# sun_path, all of them path with no NUL), and the module reads the path up to
# the first NUL instead of by the length the kernel returns, so it runs past
# sun_path into memory it never cleared. The address shown is the first 104
# bytes of the path followed by whatever bytes lie there before a NUL: it is
# never the path, and it is always longer than DIRECT_MAX, which is what
# assert_address_fits catches on the kernel's address of every bind. A
# connect in the widened form leaves no such address on its own socket (a
# client is not bound), so the connects keep the widened form; the client's
# peer address and the accepted stream's addresses are at least readable
# without an error.
def test_the_address_of_a_widened_bind_is_cut_short(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 200)
    with _route.wide_op(BIND, p) as listener:
        shown = os.fsencode(kernel_sockname(listener))
        assert shown != p
        assert shown.startswith(p[:SUN_PATH_CAP]), shown
        assert len(shown) > DIRECT_MAX, shown
        with _route.wide_op(CONNECT, p) as client:
            kernel_peername(client)
            server, _ = listener.accept()
            with server:
                kernel_sockname(server)
                kernel_peername(server)


# Every Apple fallback of the connects gets every check of a route (see
# check_route: the socket at the path, data both ways, blocking / nonblocking
# connects, close-on-exec, the peer is this process, the cwd unchanged,
# nothing left in the place, the path reported as the address while every
# address the kernel holds fits sun_path, asyncio serving and cleaning up as
# for the standard's) and the nonblocking connect against a full backlog (on
# macOS a full backlog fails at once with ECONNREFUSED for a blocking connect
# too, so what differs there is the socket that comes back: it must be
# nonblocking on every route). The listener is bound by the routes through D.
#
# * the path itself (DIRECT_MAX bytes), through the socket module, bound so
#   too;
# * the widened sockaddr with the path itself: over DIRECT_MAX bytes (and no
#   relative spelling within DIRECT_MAX: under /tmp, far from the cwd) and at
#   most 253, for DIRECT_MAX + 1, 200 and 253 bytes;
# * the symlink under a short place (the plain sockaddr), a path of 600 bytes;
# * the symlink in the widened sockaddr: under a 150-byte place (the
#   spelling through it, 168 bytes, needs the widened form), a path of 600
#   bytes.
@pytest.mark.parametrize(
    "what", ["standard", "widened-104", "widened-200", "widened-253", "symlink", "symlink-widened"]
)
def test_every_apple_route(tmp: TmpDirs, what: str) -> None:
    t = tmp.new()
    place = tmp.new()
    places = [place]
    bound = routes(Route.THREAD_CWD, Route.WIDENED)
    if what == "standard":
        f = forced(what, nest(t, DIRECT_MAX), None, Route.STANDARD)
    elif what.startswith("widened-"):
        f = forced(what, nest(t, int(what[8:])), fallback_cfg(), bound, bind_cfg=d_cfg())
    elif what == "symlink":
        want = routes(Route.THREAD_CWD, Route.SYMLINK)
        f = forced(what, nest(t, 600), fallback_cfg([place]), want, bind_cfg=d_cfg())
    else:
        places = [long_place(tmp, 150), place]
        want = routes(Route.THREAD_CWD, Route.SYMLINK)
        f = forced(what, nest(t, 600), fallback_cfg(places), want, bind_cfg=d_cfg())
    if f.want(BIND) is not Route.STANDARD:
        assert_far_from_cwd(f.path)
    check_route(f, places)
    check_full_backlog(f)
    for d in places:
        assert_empty(d)


# The bind never takes the widened sockaddr, whatever the path, while the
# connects of the same path with the same configuration do. Where the routes
# through D cannot carry the bind, it ends at the fork route's place
# (ENAMETOOLONG) with nothing created. The connects reach a listener made at
# the path by the standard (renamed there):
#
# * the path itself: DIRECT_MAX + 1, 200 and 253 bytes far from the cwd, no
#   place for D. The bind is ENAMETOOLONG; the connects go in the widened
#   form (Route.WIDENED);
# * by the file name: names of 93 (one more than `R/<name>` takes), 200 and
#   253 bytes with every route on (D in the socket's directory, a short place
#   for the symlink). The bind is ENAMETOOLONG, with nothing created next to
#   the socket or in the place; the connects go through the link to the file.
def test_an_apple_bind_never_takes_the_widened_sockaddr(tmp: TmpDirs) -> None:
    t = tmp.new()
    for i, total in enumerate((DIRECT_MAX + 1, 200, 253)):
        p = nest(t, total, f"{i}.sock".encode())
        assert_far_from_cwd(p)
        c = fallback_cfg()
        before = listing(parent(p))
        with raises_errno(errno.ENAMETOOLONG):
            _route.run_long(BIND, p, c)
        assert listing(parent(p)) == before
        with listener_at(p) as listener:
            for req in (CONNECT, CONNECT_NONBLOCKING):
                client, r = _route.run_long(req, p, c)
                with client:
                    assert r is Route.WIDENED, (total, req)
                    accept_ping(listener, client)

    short = tmp.new()
    c = d_cfg(links=[short])
    for n in (DIRECT_MAX - _route.IN_OWN_DIR_LEN + 1, 200, 253):
        p = nest(t, 600, b"n" * n)
        before = listing(parent(p))
        mark_untouched(short)
        with raises_errno(errno.ENAMETOOLONG):
            _route.run_long(BIND, p, c)
        assert listing(parent(p)) == before
        assert_untouched(short)
        with listener_at(p) as listener:
            client, r = _route.run_long(CONNECT, p, c)
            with client:
                assert r is Route.SYMLINK, n
                accept_ping(listener, client)
    assert_empty(short)


# A failure on the widened route is the kernel's error for the path (ENOENT
# for a missing socket, ECONNREFUSED for a socket nobody listens on): EINVAL /
# ENAMETOOLONG are what XNU answers for an address it will not take, these
# are not, so none of them is taken as "the kernel refuses the widened form".
# Nothing after the route is tried: the place for the symlink stays
# untouched.
def test_a_failure_on_the_widened_route_is_not_retried(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 200)
    place = tmp.new()
    c = fallback_cfg([place])
    stale = with_name(p, b"stale.sock")
    stale_socket(stale)
    mark_untouched(place)
    for req in (CONNECT, CONNECT_NONBLOCKING):
        with pytest.raises(FileNotFoundError):
            _route.run_long(req, p, c)
        with pytest.raises(ConnectionRefusedError):
            _route.run_long(req, stale, c)
    assert_untouched(place)
    with listener_at(p) as listener:
        client, r = _route.run_long(CONNECT, p, c)
        with client:
            assert r is Route.WIDENED
            accept_ping(listener, client)
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
# unusable, not an error, and every later step of a connect judges lengths by
# the plain sockaddr:
#
# * a 200-byte path that a connect would reach in the widened form goes
#   through the link to the file under the short place instead; the 150-byte
#   place before it (whose spelling would need the widened form) is passed
#   over untouched;
# * a connect to a 600-byte path (too long for the widened form itself) meets
#   the refusal under the 150-byte place (the link there is made, the widened
#   call refused, the link removed) and goes on under the short place in the
#   plain sockaddr.
def test_a_kernel_refusing_the_widened_form_moves_on_in_the_plain_sockaddr(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    refuse_wide(monkeypatch)
    t = tmp.new()
    long150 = long_place(tmp, 150)
    short = tmp.new()
    c = fallback_cfg([long150, short])

    p = nest(t, 200, b"a.sock")
    mark_untouched(long150)
    with listener_at(p) as listener:
        client, r = _route.run_long(CONNECT, p, c)
        with client:
            assert r is Route.SYMLINK
            accept_ping(listener, client)
    assert_untouched(long150)

    q = nest(t, 600, b"b.sock")
    with listener_at(q) as listener:
        client, r = _route.run_long(CONNECT, q, c)
        with client:
            assert r is Route.SYMLINK
            accept_ping(listener, client)
    # The link under the 150-byte place was made and removed again.
    assert os.stat(long150).st_mtime != UNTOUCHED
    assert_empty(long150)
    assert_empty(short)
