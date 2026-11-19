"""The connect order of a long path (hub SPEC "connect の順"): which route
is tried first on each system, every route noted as it is tried (skipped
with why, or as the one whose connect failed), and D made at most once per
call and shared by the routes that use it.

The routes themselves are checked one by one elsewhere (``test_through_d.py``,
``test_proc_held.py``, ``test_routes_*.py``, ``test_symlink.py``); here only
the order and what it notes."""

from __future__ import annotations

import errno
import os
import re
import socket

import pytest
from _support import (
    HAS_PROC,
    NO_PROC,
    PROC,
    TmpDirs,
    accept_ping,
    assert_empty,
    assert_far_from_cwd,
    assert_untouched,
    d_cfg,
    d_route_here,
    dir_id,
    far_connect_here,
    listener_at,
    listing,
    mark_untouched,
    nest,
    parent,
    raises_errno,
    stale_socket,
    thread_cwd_usable,
    with_name,
)

from sockat import _route
from sockat._route import CONNECT, CONNECT_NONBLOCKING, NO_PLACE_FOR_D, RANDOM_LEN, Reached, Route, Trail
from sockat._sys import IS_APPLE, IS_FREEBSD, IS_LINUX

NAME_PAST_PROC_DIRECT = b"n" * 93
"""A file name that does not fit behind ``/proc/self/fd/<n>/`` (proc-direct,
about 17 bytes in front; nor the /proc means, 11 more) but fits ``R/<name>``
(the thread's own cwd: 104 of 107 bytes)."""

NAME_PAST_EVERY_D_ROUTE = b"n" * 100
"""A file name that fits neither behind ``/proc/self/fd/<n>/`` nor in
``R/<name>``: only proc-held and the symlink carry it (Linux)."""


def far(tmp: TmpDirs, total: int = 600, name: bytes = b"s.sock") -> bytes:
    """A socket path of ``total`` bytes under /tmp, far from the cwd (no
    relative spelling fits)."""
    p = nest(tmp.new(), total, name)
    assert_far_from_cwd(p)
    return p


def connect(p: bytes, cfg: _route.Config, req: _route.Request = CONNECT) -> tuple[Reached, Trail]:
    """A connect to ``p`` (longer than the ``socket`` module takes) by the
    connect order with ``cfg``, and the trail it noted."""
    trail = Trail()
    return _route.reach_long(req, p, cfg, trail), trail


def noted(skipped: list[tuple[Route, str]] | tuple[tuple[Route, str], ...]) -> list[Route]:
    """The routes of a trail's ``skipped``, after checking every reason says
    something."""
    assert all(why for _, why in skipped), skipped
    return [r for r, _ in skipped]


def stale_named(p: bytes) -> None:
    """A socket file nobody listens on at ``p``, whatever the length of its
    file name: made by :func:`_support.stale_socket` under a short name
    next to it and renamed (the inode, and so the refusal, stays)."""
    short = with_name(p, b"st.sock")
    stale_socket(short)
    os.rename(short, p)


def count_made_d(monkeypatch: pytest.MonkeyPatch, *, refuse: bool = False) -> list[tuple[int, int]]:
    """Record the place (``(st_dev, st_ino)``) of every D a route tries to
    make, in order; with ``refuse``, refuse each (``EACCES``, as a place
    where D cannot be made does)."""
    tried: list[tuple[int, int]] = []
    real = _route.mkdir_at

    def mkdir_at(base_fd: int, name: bytes) -> None:
        st = os.fstat(base_fd)
        tried.append((st.st_dev, st.st_ino))
        if refuse:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        real(base_fd, name)

    monkeypatch.setattr(_route, "mkdir_at", mkdir_at)
    return tried


def thread_cwd_refused_after_d(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the thread's own cwd look usable before D is made and refuse it
    once tried (``enter_dir_in_this_thread`` answering ``False``, as a
    refused ``unshare`` does), whatever the system allows here: D is made for
    it, and the routes after it find D made."""
    monkeypatch.setattr(_route, "thread_cwd_possible", lambda: True)
    monkeypatch.setattr(_route, "enter_dir_in_this_thread", lambda _fd: False)


# ----- the order -----


# The connect order of each system is the SPEC's: the routes that need
# neither a write (D, R, a symlink) nor a thread first, then those that make
# D or start a thread, then the symlink. /proc decides Linux's order.
def test_the_connect_order_of_this_system() -> None:
    order = _route.connect_order
    if IS_FREEBSD:
        assert order(True) == order(False) == (Route.AT_DIRECT, Route.AT)
    elif IS_LINUX:
        assert order(True) == (Route.PROC_DIRECT, Route.THREAD_CWD, Route.PROC, Route.PROC_HELD, Route.SYMLINK_FILE)
        assert order(False) == (Route.RELATIVE, Route.THREAD_CWD, Route.SYMLINK_FILE)
    elif IS_APPLE:
        assert order(True) == order(False) == (Route.RELATIVE, Route.WIDENED, Route.THREAD_CWD, Route.SYMLINK_FILE)
    else:
        assert order(True) == order(False) == (Route.RELATIVE, Route.SYMLINK_FILE)


# The routes run in the order of the system (with /proc as found: present,
# and on Linux also made to look absent), one after the other: each that
# cannot be used is noted as skipped, in order, after the standard; the first
# that carries the connect is the route; nothing after it runs. Every route
# is stood in for (the last connects to a listener at a short path, the
# others answer that they cannot be used), so the order alone is seen,
# whatever the system allows.
@pytest.mark.parametrize("proc", [PROC, NO_PROC], ids=["proc", "no-proc"])
def test_the_routes_run_in_order_and_each_is_noted(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, proc: bytes) -> None:
    if proc == PROC and not HAS_PROC:
        pytest.skip("/proc is Linux's")
    p = far(tmp)
    order = _route.connect_order(proc == PROC)
    short = tmp.new() + b"/l.sock"
    ran: list[Route] = []
    for i, route in enumerate(order):

        def stand_in(
            call: _route.ConnectCall, route: Route = route, last: bool = i == len(order) - 1
        ) -> socket.socket | str:
            ran.append(route)
            return _route.plain_op(call.req, short) if last else "stood in for as unusable"

        monkeypatch.setitem(_route.CONNECT_ROUTES, route, stand_in)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(short)
        listener.listen()
        done, trail = connect(p, d_cfg(proc=proc))
        with done.sock as client:
            accept_ping(listener, client)
    assert ran == list(order)
    assert done.route is order[-1]
    assert noted(done.skipped) == [Route.STANDARD, *order[:-1]]
    assert done.losses == ()
    assert trail.trying is order[-1]


# ----- the first route that can be used -----


# Where /proc can be used, a connect goes by proc-direct first: nothing is
# made (no D in the place for it, no symlink), and only the standard is
# skipped. A file name past proc-direct goes on to the thread's own cwd where
# the system allows it (D made), else past the /proc means (too long too) to
# proc-held (D made too).
@pytest.mark.skipif(not HAS_PROC, reason="/proc is Linux's")
def test_with_proc_the_first_route_is_proc_direct(tmp: TmpDirs) -> None:
    d_place = tmp.new()
    links = tmp.new()
    c = d_cfg((d_place,), [links])
    p = far(tmp)
    for d in (d_place, links):
        mark_untouched(d)
    with listener_at(p) as listener:
        for req in (CONNECT, CONNECT_NONBLOCKING):
            done, _ = connect(p, c, req)
            with done.sock as client:
                assert done.route is Route.PROC_DIRECT, req
                assert noted(done.skipped) == [Route.STANDARD], req
                accept_ping(listener, client)
    for d in (d_place, links):
        assert_untouched(d)

    q = far(tmp, name=NAME_PAST_PROC_DIRECT)
    with listener_at(q) as listener:
        done, _ = connect(q, c)
        with done.sock as client:
            if thread_cwd_usable():
                assert done.route is Route.THREAD_CWD
                assert noted(done.skipped) == [Route.STANDARD, Route.PROC_DIRECT]
            else:
                assert done.route is Route.PROC_HELD
                assert noted(done.skipped) == [Route.STANDARD, Route.PROC_DIRECT, Route.THREAD_CWD, Route.PROC]
            accept_ping(listener, client)
    assert_empty(d_place)
    assert_untouched(links)


# Without /proc on Linux, and on Apple, a connect goes by relative first
# (here in a child whose cwd is above the socket, see test_routes_*), then on
# Apple by the widened sockaddr: a path of 200 bytes far from the cwd goes
# that way with nothing made. One past the widened form's 253 bytes goes on
# to the thread's own cwd (D made), where the system allows it.
@pytest.mark.skipif(not (IS_APPLE or IS_LINUX), reason="the order of Linux without /proc and of Apple")
def test_without_proc_the_routes_that_make_nothing_come_first(tmp: TmpDirs) -> None:
    d_place = tmp.new()
    links = tmp.new()
    c = d_cfg((d_place,), [links], proc=NO_PROC)
    for d in (d_place, links):
        mark_untouched(d)
    if IS_APPLE:
        p = far(tmp, 200)
        with listener_at(p) as listener:
            for req in (CONNECT, CONNECT_NONBLOCKING):
                done, _ = connect(p, c, req)
                with done.sock as client:
                    assert done.route is Route.WIDENED, req
                    assert noted(done.skipped) == [Route.STANDARD, Route.RELATIVE], req
                    accept_ping(listener, client)
        for d in (d_place, links):
            assert_untouched(d)
    if not thread_cwd_usable():
        return
    q = far(tmp)
    before = [Route.STANDARD, Route.RELATIVE, *([Route.WIDENED] if IS_APPLE else [])]
    with listener_at(q) as listener:
        done, _ = connect(q, c)
        with done.sock as client:
            assert done.route is Route.THREAD_CWD
            assert noted(done.skipped) == before
            accept_ping(listener, client)
    assert_empty(d_place)
    assert_untouched(links)


# ----- D, once per call -----


# The routes that use D share the one D of the call (SPEC): where the
# thread's own cwd makes D (with R) and is then refused, the /proc means
# (too long for this name) makes nothing, and proc-held puts `s` in that same
# D rather than making another: one D made in all, holding R and `s` when
# proc-held connects through it, and removed afterwards.
@pytest.mark.skipif(not HAS_PROC, reason="the routes that share D after the thread's own cwd are Linux's")
def test_d_is_made_once_and_shared_by_the_routes_that_use_it(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    p = far(tmp, name=NAME_PAST_PROC_DIRECT)
    d_place = tmp.new()
    links = tmp.new()
    mark_untouched(links)
    thread_cwd_refused_after_d(monkeypatch)
    made = count_made_d(monkeypatch)
    held: list[list[bytes]] = []
    real = _route.plain_op

    def look_then_connect(req: _route.Request, spelling: str | bytes, *, standard: bool = False) -> socket.socket:
        [d] = listing(d_place)
        held.append(listing(d_place + b"/" + d))
        return real(req, spelling, standard=standard)

    monkeypatch.setattr(_route, "plain_op", look_then_connect)
    with listener_at(p) as listener:
        done, _ = connect(p, d_cfg((d_place,), [links]))
        with done.sock as client:
            assert done.route is Route.PROC_HELD
            assert noted(done.skipped) == [Route.STANDARD, Route.PROC_DIRECT, Route.THREAD_CWD, Route.PROC]
            accept_ping(listener, client)
    assert made == [dir_id(d_place)]
    [entries] = held
    assert b"s" in entries, entries
    [r] = [e for e in entries if e != b"s"]
    assert re.fullmatch(rb"[A-Za-z0-9]{%d}" % RANDOM_LEN, r), entries
    assert_empty(d_place)
    assert_untouched(links)


# Where D can be made nowhere, every route that uses D is skipped (for that
# reason, unless it could not be used before D was wanted, as the /proc
# means is for a name too long for it) and D is not tried again: the place
# is asked once in the whole call. The connect goes on to the symlink.
@pytest.mark.skipif(not (IS_APPLE or IS_LINUX), reason="the orders with the thread's own cwd and the symlink")
def test_where_d_can_be_made_nowhere_the_routes_that_use_it_are_skipped(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    d_place = tmp.new()
    links = tmp.new()
    monkeypatch.setattr(_route, "thread_cwd_possible", lambda: True)
    tried = count_made_d(monkeypatch, refuse=True)
    if HAS_PROC:
        p = far(tmp, name=NAME_PAST_PROC_DIRECT)
        c = d_cfg((d_place,), [links])
        before = [Route.STANDARD, Route.PROC_DIRECT, Route.THREAD_CWD, Route.PROC, Route.PROC_HELD]
        no_d = {Route.THREAD_CWD, Route.PROC_HELD}
    else:
        p = far(tmp)
        c = d_cfg((d_place,), [links], proc=NO_PROC)
        before = [Route.STANDARD, Route.RELATIVE, *([Route.WIDENED] if IS_APPLE else []), Route.THREAD_CWD]
        no_d = {Route.THREAD_CWD}
    with listener_at(p) as listener:
        done, _ = connect(p, c)
        with done.sock as client:
            assert done.route is Route.SYMLINK_FILE
            assert noted(done.skipped) == before
            assert {r for r, why in done.skipped if why == NO_PLACE_FOR_D} == no_d, done.skipped
            accept_ping(listener, client)
    assert tried == [dir_id(d_place)]
    assert_empty(d_place)
    assert_empty(links)


# ----- the route that failed -----


# A connect that ran on a route of the order and failed raises the kernel's
# error, with that route noted as the one that failed (the conformance CLI's
# `route`) and the routes before it as skipped; nothing after it runs (the
# place for the symlink stays untouched where it comes after):
#
# * the symlink, the last route (a path far from the cwd, no means of the
#   routes through D, no /proc);
# * on Apple, the widened sockaddr (a path of 200 bytes);
# * where /proc can be used, proc-direct, and proc-held for a name past every
#   other route.
@pytest.mark.skipif(not (IS_APPLE or IS_LINUX), reason="the orders with the symlink")
def test_a_failure_on_a_route_of_the_order_names_that_route(tmp: TmpDirs) -> None:
    links = tmp.new()
    cases: list[tuple[bytes, _route.Config, Route, list[Route]]] = []
    p = far(tmp)
    c = d_cfg((), [links], thread_cwd=False, proc=NO_PROC)
    cases.append((p, c, Route.SYMLINK_FILE, [Route.STANDARD, *_route.connect_order(False)[:-1]]))
    if IS_APPLE:
        cases.append((far(tmp, 200), d_cfg((), [links]), Route.WIDENED, [Route.STANDARD, Route.RELATIVE]))
    if HAS_PROC:
        cases.append((far(tmp), d_cfg((), [links]), Route.PROC_DIRECT, [Route.STANDARD]))
        held = far(tmp, name=NAME_PAST_EVERY_D_ROUTE)
        before = [Route.STANDARD, Route.PROC_DIRECT, Route.THREAD_CWD, Route.PROC]
        cases.append((held, d_cfg((tmp.new(),), [links], thread_cwd=False), Route.PROC_HELD, before))
    for path, cfg, route, before_it in cases:
        stale_named(path)
        if route is not Route.SYMLINK_FILE:
            mark_untouched(links)
        for req in (CONNECT, CONNECT_NONBLOCKING):
            trail = Trail()
            with pytest.raises(ConnectionRefusedError):
                _route.reach_long(req, path, cfg, trail)
            assert trail.trying is route, (route, req)
            assert noted(trail.skipped) == before_it, (route, req)
        if route is not Route.SYMLINK_FILE:
            assert_untouched(links)
        assert listing(parent(path)) == [os.path.basename(path)]
    assert_empty(links)


# A connect that no route of the order carries is ENAMETOOLONG with no
# route noted as having failed, and every route of the order noted as
# skipped after the standard.
@pytest.mark.skipif(not (IS_APPLE or IS_LINUX), reason="the orders with the symlink")
def test_past_the_last_route_every_route_is_noted(tmp: TmpDirs) -> None:
    p = far(tmp)
    trail = Trail()
    with raises_errno(errno.ENAMETOOLONG):
        _route.reach_long(CONNECT, p, d_cfg((), (), thread_cwd=False, proc=NO_PROC), trail)
    assert trail.trying is None
    assert noted(trail.skipped) == [Route.STANDARD, *_route.connect_order(False)]


# ----- the socket's directory that cannot be opened -----


# Where the socket's directory cannot be opened (a real directory of mode
# 0300, opened O_RDONLY as the systems other than Linux and Apple open it,
# which needs read permission), neither the bind nor the connects fail as a
# whole: only the routes that need its fd cannot be used (proc-direct and
# at-direct, noted as skipped for it; D in the socket's directory; the form
# without D), and the others carry them. The bind goes through D made in
# another place; the connect where /proc can be used goes on past
# proc-direct to the routes through D (thread-cwd, else /proc, which proc-
# direct otherwise always comes before), on FreeBSD past at-direct to at,
# elsewhere as usual. Root bypasses the permission, so the test runs for
# other users only.
@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the permission under test")
def test_a_socket_directory_that_cannot_be_opened_leaves_the_other_routes(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = d_route_here()
    if here is None:
        pytest.skip("no means of the routes through D here")
    p = far(tmp)
    os.chmod(parent(p), 0o300)
    monkeypatch.setattr(_route, "open_dir", _route._open_read_only)
    with pytest.raises(PermissionError):
        _route.open_dir(parent(p))
    d_place = tmp.new()
    c = d_cfg((d_place,), bind_symlink_file=False)
    listener, r = _route.run_long(_route.BIND, p, c)
    with listener:
        assert r is here
        done, _ = connect(p, c)
        with done.sock as client:
            if IS_FREEBSD:
                assert done.route is Route.AT
                assert dict(done.skipped)[Route.AT_DIRECT] == _route.SOCKET_DIR_NOT_OPENED
            elif HAS_PROC:
                assert done.route is (Route.THREAD_CWD if thread_cwd_usable() else Route.PROC)
                assert dict(done.skipped)[Route.PROC_DIRECT] == _route.SOCKET_DIR_NOT_OPENED
            else:
                assert done.route is far_connect_here()
            accept_ping(listener, client)
    assert_empty(d_place)
