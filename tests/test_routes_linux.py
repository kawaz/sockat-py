"""The Linux routes: ``/proc/self/fd/<dirfd>/<name>`` where /proc can be
used, the POSIX order where it cannot (no /proc, or a file name too long to
fit behind ``/proc/self/fd/<n>/``), and every route forced and checked.

"No /proc" is stood in for by pointing ``Config.proc_self_fd`` at a path
that does not exist, or at an empty directory (a /proc mount point with
nothing mounted on it); the real /proc is untouched."""

from __future__ import annotations

import errno
import os
import re
import sys

import pytest
from _support import (
    NO_PROC,
    TmpDirs,
    accept_ping,
    assert_far_from_cwd,
    assert_untouched,
    check_full_backlog,
    check_route,
    check_unreachable,
    forced,
    is_socket,
    kernel_sockname,
    listener_at,
    mark_untouched,
    nest,
    nest_tail,
    posix_cfg,
    raises_errno,
    run_child,
    stale_socket,
    symlink_route,
    thread_cwd_usable,
    with_name,
)

from sockat import _route
from sockat._route import BIND, CONNECT, CONNECT_NONBLOCKING, Config, Route
from sockat._sys import DIRECT_MAX

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the /proc route is Linux's")

PROC = b"/proc/self/fd"


def proc_cfg(bases: list[bytes] | None = None, *, thread_cwd: bool = False) -> Config:
    """The real /proc, ``bases`` as the places for the symlink, the
    thread-cwd route off unless asked for."""
    return Config(tmp_bases=tuple(bases or ()), proc_self_fd=PROC, thread_cwd=thread_cwd)


def no_proc(t: bytes, empty: bool) -> bytes:
    """A stand-in for ``/proc/self/fd`` on a system without /proc: a path
    under ``t`` with nothing at ``<it>/<fd>``. ``empty`` picks an existing,
    empty directory over a path that does not exist at all."""
    p = t + b"/proc/self/fd"
    if empty:
        os.makedirs(p)
    return p


# Every path over DIRECT_MAX bytes takes /proc/self/fd/<dirfd>/<name> on
# Linux, however long the directory part is (the directory never goes into the
# sockaddr, only the fd number and the file name). With /proc there, nothing
# else is tried: the place for the symlink given here stays untouched.
@pytest.mark.parametrize("total", [DIRECT_MAX + 1, 300, 1000])
def test_long_paths_take_the_proc_route(tmp: TmpDirs, total: int) -> None:
    p = nest(tmp.new(), total)
    place = tmp.new()
    mark_untouched(place)
    c = proc_cfg([place], thread_cwd=True)
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is Route.PROC_FD
        assert is_socket(p)
        for req in (CONNECT, CONNECT_NONBLOCKING):
            client, r = _route.run_long(req, p, c)
            with client:
                assert r is Route.PROC_FD
                accept_ping(listener, client)
    assert_untouched(place)


# Why the listener reports the path rather than the kernel's address: the
# kernel records the spelling that was bound, /proc/self/fd/<n>/<name>, and the
# directory fd <n> is closed as soon as the bind returns. The number is then
# free, and an fd the process opens later takes it (POSIX: open takes the
# lowest free number; files are opened here until one gets it, in case a lower
# number was freed meanwhile), so the spelling names whatever that is: here a
# regular file, which makes os.stat of the spelling NotADirectoryError. That is what asyncio's
# start_unix_server(sock=) did with a listener bound before the event loop
# was made (the loop's own fds took the number; Python 3.13 and later stat
# the listener's address at the start). So getsockname reports the path,
# while the kernel's spelling stays readable through the standard method
# (socket.socket.getsockname), within sun_path.
def test_the_kernel_holds_the_proc_spelling_whose_fd_is_reused_so_the_path_is_reported(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 200)
    listener, r = _route.run_long(BIND, p, proc_cfg())
    with listener:
        assert r is Route.PROC_FD
        assert listener.getsockname() == os.fsdecode(p)
        kernel = os.fsencode(kernel_sockname(listener))
        m = re.fullmatch(rb"/proc/self/fd/(\d+)/s\.sock", kernel)
        assert m is not None, kernel
        n = int(m.group(1))
        opened: list[int] = []
        try:
            while not opened or opened[-1] != n:
                opened.append(os.open(with_name(p, b"plain-file"), os.O_RDONLY | os.O_CREAT, 0o600))
                assert opened[-1] <= n, "the premise: the closed directory fd's number is free"
            with pytest.raises(NotADirectoryError):
                os.stat(kernel)
        finally:
            for fd in opened:
                os.close(fd)
        assert os.stat(listener.getsockname()).st_ino == os.lstat(p).st_ino


# A failure on the /proc route is the kernel's error for the path (ENOENT for
# a missing directory, from opening it, and for a missing socket; EADDRINUSE
# for an existing one; ECONNREFUSED for a socket nobody listens on), and
# nothing else is tried: the place for the symlink stays untouched (the
# thread-cwd route is on too).
def test_a_failure_on_the_proc_route_is_not_retried(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 300)
    place = tmp.new()
    c = proc_cfg([place], thread_cwd=True)
    stale = with_name(p, b"stale.sock")
    stale_socket(stale)
    mark_untouched(place)
    for req in (BIND, CONNECT, CONNECT_NONBLOCKING):
        with pytest.raises(FileNotFoundError):
            _route.run_long(req, with_name(p, b"nope/s.sock"), c)
    with pytest.raises(FileNotFoundError):
        _route.run_long(CONNECT, p, c)
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is Route.PROC_FD
        with raises_errno(errno.EADDRINUSE):
            _route.run_long(BIND, p, c)
    for req in (CONNECT, CONNECT_NONBLOCKING):
        with pytest.raises(ConnectionRefusedError):
            _route.run_long(req, stale, c)
    assert_untouched(place)


# "/proc/self/fd/<n>/" takes about 17 bytes of the 107 that sun_path holds, so
# a file name of 95 bytes does not fit behind it. That makes the /proc route
# unusable, as no /proc does, and the POSIX order runs with the real /proc in
# place (the path is far from the cwd, so not relative):
#
# * bind: the link to the directory does not fit either (/tmp/sockat.X/ is 23
#   bytes, + 95 > DIRECT_MAX), so the thread-cwd route binds it where it can
#   be used (the name fits its sockaddr); where not, ENAMETOOLONG. A name of
#   108 bytes fits no sockaddr at all: ENAMETOOLONG either way, nothing made;
# * connect: the link to the socket file takes it without either;
#   with no place for the link, the thread-cwd route where it can be used,
#   ENAMETOOLONG where not.
#
# A name of 80 bytes still fits behind /proc and takes that route.
def test_a_file_name_that_does_not_fit_behind_proc_takes_the_posix_order(tmp: TmpDirs) -> None:
    t = tmp.new()
    tmp_place = proc_cfg([b"/tmp"], thread_cwd=True)
    no_place = proc_cfg(thread_cwd=True)
    usable = thread_cwd_usable()

    p = t + b"/" + b"n" * 95
    assert_far_from_cwd(p)
    if usable:
        listener, r = _route.run_long(BIND, p, tmp_place)
        assert r is Route.THREAD_CWD
    else:
        with raises_errno(errno.ENAMETOOLONG):
            _route.run_long(BIND, p, tmp_place)
        assert os.listdir(t) == []
        listener = listener_at(p)
    with listener:
        assert is_socket(p)
        for req in (CONNECT, CONNECT_NONBLOCKING):
            client, r = _route.run_long(req, p, tmp_place)
            with client:
                assert r is Route.SYMLINK_TO_FILE, req
                accept_ping(listener, client)
        if usable:
            client, r = _route.run_long(CONNECT, p, no_place)
            with client:
                assert r is Route.THREAD_CWD
                accept_ping(listener, client)
    if not usable:
        os.unlink(p)
        check_unreachable(p, no_place)

    longest = t + b"/" + b"m" * DIRECT_MAX
    if usable:
        listener, r = _route.run_long(BIND, longest, tmp_place)
        listener.close()
        assert r is Route.THREAD_CWD
        assert is_socket(longest)
    with raises_errno(errno.ENAMETOOLONG):
        _route.run_long(BIND, t + b"/" + b"o" * (DIRECT_MAX + 1), tmp_place)
    assert not os.path.lexists(t + b"/" + b"o" * (DIRECT_MAX + 1))

    fits = t + b"/" + b"f" * 80
    listener, r = _route.run_long(BIND, fits, tmp_place)
    with listener:
        assert r is Route.PROC_FD
        client, r = _route.run_long(CONNECT, fits, tmp_place)
        with client:
            assert r is Route.PROC_FD
            accept_ping(listener, client)


# Why a bind never goes through a link to the socket file: Linux does not
# follow a symlink in the last component of a bind, it finds the name taken
# (EADDRINUSE), even when the link dangles. A connect follows it. Pinned (with
# the socket module itself) so the reason for the two shapes of the symlink
# route stays checkable.
def test_linux_binds_not_through_a_link_to_the_file_but_connects_through_it(tmp: TmpDirs) -> None:
    import socket

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


# Without /proc the /proc route cannot be used at all, so Linux takes the POSIX
# order (as the Unix systems without a dirfd-relative call do) instead of
# failing, for both shapes of "no /proc" (no such directory; an empty one):
# relative → symlink → thread cwd → the fork route's place. Each step is
# reached by taking the earlier ones away:
#
# * a path whose spelling relative to the cwd fits: the relative route (in a
#   child whose cwd is near the socket);
# * a path far from the cwd with a short place: the symlink route;
# * no place: the thread-cwd route where the system allows it;
# * the thread-cwd route off (or refused): ENAMETOOLONG, nothing made.
#
# With the real /proc, the same paths take the /proc route.
@pytest.mark.parametrize("empty", [False, True])
def test_without_proc_the_posix_order_runs(tmp: TmpDirs, empty: bool) -> None:
    t = tmp.new()
    none = no_proc(t, empty)
    place = tmp.new()

    near = nest_tail(t, DIRECT_MAX, b"rel.sock")
    out = run_child("bind_report", t, {"PATH": near, "PROC": none})
    assert out["route"] == Route.RELATIVE.value
    assert is_socket(near)

    far = nest(t, 600, b"far.sock")
    assert_far_from_cwd(far)
    c = posix_cfg([place], proc=none)
    listener, r = _route.run_long(BIND, far, c)
    with listener:
        assert r is Route.SYMLINK_TO_DIR
        assert is_socket(far)
        client, r = _route.run_long(CONNECT, far, c)
        with client:
            assert r is Route.SYMLINK_TO_FILE
            accept_ping(listener, client)
        client, r = _route.run_long(CONNECT, far, proc_cfg())
        with client:
            assert r is Route.PROC_FD
            accept_ping(listener, client)

    thread = nest(t, 600, b"thread.sock")
    if thread_cwd_usable():
        listener, r = _route.run_long(BIND, thread, posix_cfg(proc=none, thread_cwd=True))
        with listener:
            assert r is Route.THREAD_CWD
            assert is_socket(thread)
        os.unlink(thread)
    check_unreachable(thread, posix_cfg(proc=none))


# Every Linux route but the relative one (forced in a child process in
# test_routes_posix) gets every check of a route (see check_route: the socket
# at the path, data both ways, blocking / nonblocking connects, close-on-exec,
# the peer is this process, the cwd unchanged, nothing left in the place, the
# path reported as the address, asyncio serving and cleaning up as for the
# standard's, the listener bound before the loop and in it) and the
# nonblocking connect against a full backlog (a blocking connect would
# wait for room on Linux; the nonblocking one must fail at once with EAGAIN on
# every route):
#
# * the path itself (DIRECT_MAX bytes), through the socket module;
# * /proc (300 bytes);
# * with /proc made to look absent, the symlink under a short place (600
#   bytes);
# * the thread-cwd route (600 bytes, no place) where the system allows it.
@pytest.mark.parametrize("what", ["direct", "proc", "symlink", "thread-cwd"])
def test_every_linux_route(tmp: TmpDirs, what: str) -> None:
    t = tmp.new()
    place = tmp.new()
    if what == "direct":
        f = forced(what, nest(t, DIRECT_MAX), None, Route.DIRECT)
    elif what == "proc":
        f = forced(what, nest(t, 300), proc_cfg(), Route.PROC_FD)
    elif what == "symlink":
        f = forced(what, nest(t, 600), posix_cfg([place]), symlink_route)
    else:
        if not thread_cwd_usable():
            pytest.skip("the system does not give a thread a cwd of its own here")
        f = forced(what, nest(t, 600), posix_cfg(thread_cwd=True), Route.THREAD_CWD)
    if what in ("symlink", "thread-cwd"):
        assert_far_from_cwd(f.path)
    check_route(f, [place])
    check_full_backlog(f)


# With /proc absent the fork route's place is reached as on the other Unix
# systems, and it is ENAMETOOLONG for every request with nothing made, while a
# missing directory is still ENOENT (it is opened first). NO_PROC is the
# stand-in used by the shared POSIX-order tests, checked here once to be one.
def test_without_proc_the_end_of_the_order_is_enametoolong(tmp: TmpDirs) -> None:
    assert not os.path.exists(NO_PROC)
    p = nest(tmp.new(), 300)
    check_unreachable(p, posix_cfg())
    with pytest.raises(FileNotFoundError):
        _route.run_long(BIND, with_name(p, b"nope/s.sock"), posix_cfg())
