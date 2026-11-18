"""The Linux routes: the central route's means (thread cwd, /proc) for the
binds and connects, and a connect's fallbacks where /proc can be used
(``/proc/self/fd/<dirfd>/<name>``, the fd-held form, the symlink) and where it
cannot (the relative spelling, the symlink). The fd-held form is in
``test_fd_held.py``, the central route in general in ``test_central.py``.

"No /proc" is stood in for by pointing ``Config.proc_self_fd`` at a path
that does not exist, or at an empty directory (a /proc mount point with
nothing mounted on it); the real /proc is untouched."""

from __future__ import annotations

import errno
import os
import re
import socket
import sys

import pytest
from _support import (
    NO_PROC,
    PROC,
    TmpDirs,
    accept_ping,
    assert_far_from_cwd,
    assert_untouched,
    central_cfg,
    check_full_backlog,
    check_route,
    check_unreachable,
    fallback_cfg,
    forced,
    is_socket,
    kernel_sockname,
    listener_at,
    listing,
    mark_untouched,
    nest,
    nest_tail,
    parent,
    raises_errno,
    routes,
    run_child,
    stale_socket,
    thread_cwd_usable,
    with_name,
)

from sockat import _route
from sockat._route import BIND, CONNECT, CONNECT_NONBLOCKING, SOCKET_DIR, Config, Route
from sockat._sys import DIRECT_MAX

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the /proc routes are Linux's")


def proc_fallback_cfg(links: list[bytes] | None = None, *, own: tuple[bytes, ...] = ()) -> Config:
    """The real /proc, no means of the central route but /proc (the thread's
    own cwd off), ``own`` as the places for D (none: the central route and
    the fd-held form cannot be used, and a bind takes the form without D),
    ``links`` as the places for the symlink."""
    return Config(own_dir_places=own, link_places=tuple(links or ()), proc_self_fd=PROC, thread_cwd=False)


def no_proc(t: bytes, empty: bool) -> bytes:
    """A stand-in for ``/proc/self/fd`` on a system without /proc: a path
    under ``t`` with nothing at ``<it>/<fd>``. ``empty`` picks an existing,
    empty directory over a path that does not exist at all."""
    p = t + b"/proc/self/fd"
    if empty:
        os.makedirs(p)
    return p


# Every path over DIRECT_MAX bytes, however long its directory part, is bound
# and connected to by the central route through /proc where the thread's own
# cwd is off (the directory never goes into the sockaddr, only the fd number,
# R and the file name), and by the thread's own cwd where it is on and can be
# used. Nothing else is tried: the place for the symlink stays untouched.
@pytest.mark.parametrize("total", [DIRECT_MAX + 1, 300, 1000])
@pytest.mark.parametrize("thread", [False, True])
def test_long_paths_take_the_central_route(tmp: TmpDirs, total: int, thread: bool) -> None:
    if thread and not thread_cwd_usable():
        pytest.skip("the system does not give a thread a cwd of its own here")
    p = nest(tmp.new(), total)
    place = tmp.new()
    mark_untouched(place)
    c = central_cfg(links=[place], thread_cwd=thread)
    want = Route.THREAD_CWD if thread else Route.PROC_OWN_DIR
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is want
        assert is_socket(p)
        for req in (CONNECT, CONNECT_NONBLOCKING):
            client, r = _route.run_long(req, p, c)
            with client:
                assert r is want
                accept_ping(listener, client)
    assert_untouched(place)


# Where the central route is not used (no place for D), a connect takes
# /proc/self/fd/<dirfd>/<name> with the socket's directory opened O_PATH, for
# any length of the directory part; nothing after it is tried (the place for
# the symlink stays untouched). No bind takes it (its spelling would name
# whatever reuses the fd number afterwards): the bind of the same path takes
# the form without D, /proc/self/fd/<dirfd>/.sockat.XXXXXXXXXX/<name> through
# R in the socket's directory, removed again, so only the socket file is left
# next to it.
@pytest.mark.parametrize("total", [DIRECT_MAX + 1, 300, 1000])
def test_a_connect_falls_back_to_proc(tmp: TmpDirs, total: int) -> None:
    p = nest(tmp.new(), total)
    place = tmp.new()
    mark_untouched(place)
    c = proc_fallback_cfg([place])
    before = listing(parent(p))
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is Route.PROC_WITHOUT_D
        kernel = os.fsencode(kernel_sockname(listener))
        assert re.fullmatch(rb"/proc/self/fd/\d+/\.sockat\.[A-Za-z0-9]{10}/s\.sock", kernel), kernel
        assert listing(parent(p)) == sorted([*before, b"s.sock"])
        for req in (CONNECT, CONNECT_NONBLOCKING):
            client, r = _route.run_long(req, p, c)
            with client:
                assert r is Route.PROC_FD
                accept_ping(listener, client)
    assert_untouched(place)


# A failure on the /proc fallback is the kernel's error for the path (ENOENT
# for a missing directory, from opening it, and for a missing socket;
# ECONNREFUSED for a socket nobody listens on), and nothing else is tried: the
# place for the symlink stays untouched.
def test_a_failure_on_the_proc_fallback_is_not_retried(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 300)
    place = tmp.new()
    c = proc_fallback_cfg([place])
    stale = with_name(p, b"stale.sock")
    stale_socket(stale)
    mark_untouched(place)
    for req in (CONNECT, CONNECT_NONBLOCKING):
        with pytest.raises(FileNotFoundError):
            _route.run_long(req, with_name(p, b"nope/s.sock"), c)
        with pytest.raises(FileNotFoundError):
            _route.run_long(req, p, c)
        with pytest.raises(ConnectionRefusedError):
            _route.run_long(req, stale, c)
    assert_untouched(place)


# "/proc/self/fd/<n>/" takes about 17 bytes of the 107 that sun_path holds, so
# a file name of 95 bytes does not fit behind it, and a connect goes on to the
# fd-held form (a place for D given) or, with no place for D, the symlink
# (here under /tmp). A name of 80 bytes still fits behind /proc and takes that
# fallback.
def test_a_file_name_that_does_not_fit_behind_proc_goes_on_to_the_next_fallback(tmp: TmpDirs) -> None:
    t = tmp.new()
    p = t + b"/" + b"n" * 95
    assert_far_from_cwd(p)
    with listener_at(p) as listener:
        for c, want in (
            (proc_fallback_cfg(own=(tmp.new(),)), Route.FD_HELD),
            (proc_fallback_cfg([b"/tmp"]), Route.SYMLINK_TO_FILE),
        ):
            for req in (CONNECT, CONNECT_NONBLOCKING):
                client, r = _route.run_long(req, p, c)
                with client:
                    assert r is want, req
                    accept_ping(listener, client)
    fits = t + b"/" + b"f" * 80
    with listener_at(fits) as listener:
        client, r = _route.run_long(CONNECT, fits, proc_fallback_cfg([b"/tmp"]))
        with client:
            assert r is Route.PROC_FD
            accept_ping(listener, client)


# Why a connect's symlink goes to the socket file and no bind goes through
# one: Linux does not follow a symlink in the last component of a bind, it
# finds the name taken (EADDRINUSE), even when the link dangles. A connect
# follows it. Pinned (with the socket module itself) so the reason stays
# checkable.
def test_linux_binds_not_through_a_link_to_the_file_but_connects_through_it(tmp: TmpDirs) -> None:
    t = tmp.new()
    target = t + b"/s.sock"
    link = t + b"/link"
    os.symlink(target, link)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s, raises_errno(errno.EADDRINUSE):
        s.bind(link)
    assert not is_socket(target)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(target)
        listener.listen()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(link)
            accept_ping(listener, client)


# Without /proc (no such directory; an empty one) the central route has only
# the thread's own cwd (D opened O_RDONLY and set to 0700 by fchmod, as an
# O_PATH fd takes no fchmod), and a connect's fallbacks are those of the
# systems without /proc: relative, then symlink. Each step is reached by
# taking the earlier ones away:
#
# * the thread's own cwd, where the system allows it: binds and connects;
# * with it off: the bind is ENAMETOOLONG with nothing made; a connect to a
#   path whose relative spelling fits takes the relative fallback (in a child
#   whose cwd is near the socket); one far from the cwd with a short place
#   takes the symlink; with no place, ENAMETOOLONG.
@pytest.mark.parametrize("empty", [False, True])
def test_without_proc_the_order_of_the_systems_without_it_runs(tmp: TmpDirs, empty: bool) -> None:
    t = tmp.new()
    none = no_proc(t, empty)
    place = tmp.new()

    far = nest(t, 600, b"far.sock")
    assert_far_from_cwd(far)
    if thread_cwd_usable():
        c = central_cfg((SOCKET_DIR,), proc=none)
        listener, r = _route.run_long(BIND, far, c)
        with listener:
            assert r is Route.THREAD_CWD
            client, r = _route.run_long(CONNECT, far, c)
            with client:
                assert r is Route.THREAD_CWD
                accept_ping(listener, client)
        os.unlink(far)

    c = central_cfg((SOCKET_DIR,), [place], thread_cwd=False, proc=none)
    before = listing(parent(far))
    with raises_errno(errno.ENAMETOOLONG):
        _route.run_long(BIND, far, c)
    assert listing(parent(far)) == before
    with listener_at(far) as listener:
        client, r = _route.run_long(CONNECT, far, c)
        with client:
            assert r is Route.SYMLINK_TO_FILE
            accept_ping(listener, client)
    os.unlink(far)
    check_unreachable(far, fallback_cfg(proc=none))

    near = nest_tail(t, DIRECT_MAX, b"rel.sock")
    with listener_at(near) as listener:
        out = run_child("connect_report", t, {"PATH": near})
        assert out["route"] == Route.RELATIVE.value
        listener.setblocking(False)
        listener.accept()[0].close()


# The fallbacks of a connect on Linux get every check of a route (see
# check_route: the socket at the path, data both ways, blocking / nonblocking
# connects, close-on-exec, the peer is this process, the cwd unchanged,
# nothing left in the place, the path reported as the address, asyncio
# serving and cleaning up as for the standard's) and the nonblocking connect
# against a full backlog (a blocking connect would wait for room on Linux;
# the nonblocking one must fail at once with EAGAIN on every route). The
# listener is bound by the central route:
#
# * the path itself (DIRECT_MAX bytes), through the socket module;
# * /proc (300 bytes);
# * with /proc made to look absent, the symlink under a short place (600
#   bytes).
@pytest.mark.parametrize("what", ["direct", "proc", "symlink"])
def test_every_linux_fallback(tmp: TmpDirs, what: str) -> None:
    t = tmp.new()
    place = tmp.new()
    bind = central_cfg()
    bound = Route.THREAD_CWD if thread_cwd_usable() else Route.PROC_OWN_DIR
    if what == "direct":
        f = forced(what, nest(t, DIRECT_MAX), None, Route.DIRECT)
    elif what == "proc":
        f = forced(what, nest(t, 300), proc_fallback_cfg(), routes(bound, Route.PROC_FD), bind_cfg=bind)
    else:
        want = routes(bound, Route.SYMLINK_TO_FILE)
        f = forced(what, nest(t, 600), fallback_cfg([place], proc=NO_PROC), want, bind_cfg=bind)
        assert_far_from_cwd(f.path)
    check_route(f, [place])
    check_full_backlog(f)


# With /proc absent and no place for D or the symlink, nothing reaches a long
# path: ENAMETOOLONG for every request with nothing made, while a missing
# directory is still ENOENT (it is opened first). NO_PROC is the stand-in used
# by the shared tests, checked here once to be one.
def test_without_proc_the_end_of_the_order_is_enametoolong(tmp: TmpDirs) -> None:
    assert not os.path.exists(NO_PROC)
    p = nest(tmp.new(), 300)
    check_unreachable(p, fallback_cfg())
    with pytest.raises(FileNotFoundError):
        _route.run_long(BIND, with_name(p, b"nope/s.sock"), fallback_cfg())
