"""The POSIX route order (every system but FreeBSD; on Linux when the /proc
route cannot be used, made so here by pointing ``proc_self_fd`` at a path
that does not exist): the relative spelling, the thread-cwd route (Linux,
Apple), and the fork route's place, where this implementation (which has no
fork route) answers ``ENAMETOOLONG``. The symlink route is in
``test_symlink.py``.

Tests that need a particular cwd or ``$PWD`` run in a child process
(``_children.py``)."""

from __future__ import annotations

import errno
import fcntl
import os
import socket
import sys
import threading
import types
from collections.abc import Callable

import pytest
from _support import (
    NO_PLACE,
    TmpDirs,
    accept_ping,
    assert_far_from_cwd,
    assert_untouched,
    check_full_backlog,
    check_route,
    check_unreachable,
    dir_id,
    forced,
    is_socket,
    listener_at,
    listing,
    mark_untouched,
    nest,
    nest_tail,
    parent,
    ping,
    posix_cfg,
    raises_errno,
    run_child,
    stale_socket,
    thread_cwd_usable,
    with_name,
)

from sockat import _route, _sys
from sockat._route import BIND, CONNECT, CONNECT_NONBLOCKING, Route
from sockat._sys import DIRECT_MAX, IS_APPLE, WIDE_PATH_MAX

pytestmark = pytest.mark.skipif(sys.platform.startswith("freebsd"), reason="FreeBSD takes bindat only")

needs_thread_cwd = pytest.mark.skipif(
    not thread_cwd_usable(), reason="the system does not give a thread a cwd of its own here"
)

THREAD_CONNECT_NAME_MAX = WIDE_PATH_MAX if IS_APPLE else DIRECT_MAX
"""The longest file name the thread-cwd route connects to: what a sockaddr
carries (the widened one on Apple, where the kernel takes it). A bind takes
names up to DIRECT_MAX everywhere: it never uses the widened form."""


# ----- the relative spelling -----


# Route order 1: a path over DIRECT_MAX bytes whose spelling relative to the
# cwd is at most DIRECT_MAX bytes takes the relative route (the spelling handed
# to the socket module), before anything else (on Apple, before the widened
# form). Every check of a route (the socket at the given absolute path, data
# both ways, blocking and nonblocking connects, close-on-exec, the peer is this
# process, the cwd unchanged, the path reported as the address while every
# address the kernel holds fits sun_path, asyncio serving and cleaning up as
# for the standard's; a nonblocking connect fails at once on a full backlog)
# is run on it, in a child whose cwd is the socket's ancestor. The kernel holds
# the relative spelling as the listener's address, which names the socket
# only from that cwd; the path given is reported instead.
#
# On Apple the connects also take the widened form with the relative spelling
# (a path over 253 bytes whose relative spelling is over DIRECT_MAX and at
# most 253) and get the same checks. The bind of that path does not: a bind
# never takes the widened form (the spelling it leaves on the listener would
# not fit sun_path). With the thread-cwd route off it is ENAMETOOLONG with
# nothing created; with it on, the bind goes by the file name from the
# thread-cwd route, which is how the listener for the connects is made.
def test_a_path_short_relative_to_the_cwd_takes_the_relative_route(tmp: TmpDirs) -> None:
    t = tmp.new()
    args = {"PATH": nest_tail(t, DIRECT_MAX, b"rel.sock")}
    assert len(args["PATH"]) > DIRECT_MAX
    want = "relative"
    if IS_APPLE:
        args["WPATH"] = nest_tail(t, 240, b"wrel.sock")
        assert len(args["WPATH"]) > WIDE_PATH_MAX
        want = "relative,wide-relative"
    out = run_child("relative_routes", t, args)
    assert out == {"checked": want}


def bind_in_child(cwd: bytes, pwd: bytes, path: bytes) -> tuple[str, str]:
    """Bind ``path`` in a child whose cwd is ``cwd`` and ``$PWD`` is ``pwd``;
    the route and the relative spelling (``-`` for none) it reported."""
    out = run_child("bind_report", cwd, {"PATH": path}, env={"PWD": pwd})
    return out["route"], out["rel"]


def real_and_link(t: bytes) -> tuple[bytes, bytes]:
    """``<t>/real/`` and ``<t>/link -> real``, for the cwd tests."""
    real = t + b"/real"
    os.mkdir(real)
    os.symlink(b"real", t + b"/link")
    return real, t + b"/link"


# $PWD reached through a symlink, and the path written through the same
# symlink: the path starts with $PWD, so the prefix is cut off and the
# spelling only descends (no "..", no realpath). getcwd would not have
# matched: it is the real path. The socket is in the real directory.
def test_a_path_below_pwd_through_a_symlink_is_cut_at_pwd(tmp: TmpDirs) -> None:
    t = tmp.new()
    real, link = real_and_link(t)
    p = nest_tail(link, 95)
    assert len(p) > DIRECT_MAX
    rel_want = p[len(link) + 1 :]
    route, rel = bind_in_child(link, link, p)
    assert route == Route.RELATIVE.value
    assert os.fsencode(rel) == rel_want
    assert is_socket(real + b"/" + rel_want)


# The cwd is reached through a symlink and $PWD does not name it (here: "/").
# The path is written through the symlink, so it starts with neither getcwd
# nor a usable $PWD; both sides are resolved to real paths instead and the
# spelling descends from the real cwd. (On macOS /tmp is itself a symlink to
# /private/tmp, which alone would defeat comparing the paths as written.)
def test_a_cwd_through_a_symlink_is_matched_by_real_paths(tmp: TmpDirs) -> None:
    t = tmp.new()
    real, link = real_and_link(t)
    p = nest_tail(link, 95)
    rel_want = p[len(link) + 1 :]
    route, rel = bind_in_child(link, b"/", p)
    assert route == Route.RELATIVE.value
    assert os.fsencode(rel) == rel_want
    assert is_socket(real + b"/" + rel_want)


# A symlink in the middle of the path, below the cwd: `<cwd>/link/...` where
# link -> real. Cutting the cwd off gives "link/..." (descends through the
# link); either way the spelling names the socket in the real directory and
# needs no "..". The point is the place, not the spelling.
def test_a_symlink_in_the_middle_of_the_path_still_lands_in_the_real_directory(tmp: TmpDirs) -> None:
    t = tmp.new()
    real, link = real_and_link(t)
    p = nest_tail(link, 90)
    below = p[len(link) + 1 :]
    route, rel = bind_in_child(t, t, p)
    assert route == Route.RELATIVE.value
    assert b".." not in os.fsencode(rel).split(b"/"), rel
    assert is_socket(real + b"/" + below)


# The path is in a sibling of the cwd, so the spelling climbs with "..": cwd
# <t>/a/b, path <t>/c/... gives "../../c/...". Both sides are real paths, so
# the climb goes out of the real cwd. The socket is in <t>/c.
def test_a_path_beside_the_cwd_is_spelled_with_dotdot(tmp: TmpDirs) -> None:
    t = tmp.new()
    cwd = t + b"/a/b"
    os.makedirs(cwd)
    p = nest_tail(t + b"/c", 90)
    route, rel = bind_in_child(cwd, cwd, p)
    assert route == Route.RELATIVE.value
    assert rel.startswith("../../c/"), rel
    assert is_socket(p)


# A $PWD that names another directory than the cwd must not be used: cut off
# the path, it would spell the right subpath relative to the wrong directory.
# Both directories have the same subtree here, so using $PWD would silently
# create the socket in the cwd's copy. The socket must be in the $PWD directory
# (where the path points), reached by climbing out of the real cwd.
def test_a_pwd_that_is_not_the_cwd_is_not_used(tmp: TmpDirs) -> None:
    t = tmp.new()
    cwd = t + b"/one"
    pwd = t + b"/two"
    p = nest_tail(pwd, 90)
    sub = p[len(pwd) + 1 :]
    os.makedirs(cwd + b"/" + os.path.dirname(sub))
    route, rel = bind_in_child(cwd, pwd, p)
    assert route == Route.RELATIVE.value
    assert rel.startswith("../two/"), rel
    assert is_socket(p)
    assert not is_socket(cwd + b"/" + sub)


# A cwd whose name is a byte prefix of the path's next component (cwd <t>/ab,
# path <t>/abc/...) is not a prefix of the path: cutting bytes would spell
# "c/..." and name <t>/ab/c/.... The spelling climbs instead.
def test_a_cwd_that_is_only_a_byte_prefix_is_not_cut_off(tmp: TmpDirs) -> None:
    t = tmp.new()
    cwd = t + b"/ab"
    os.mkdir(cwd)
    p = nest_tail(t + b"/abc", 90)
    route, rel = bind_in_child(cwd, cwd, p)
    assert route == Route.RELATIVE.value
    assert rel.startswith("../abc/"), rel
    assert is_socket(p)
    assert not os.path.lexists(cwd + b"/c")


# Another thread that changes the cwd between the spelling and the bind /
# connect sends the call to another directory. When the cwd is still changed
# afterwards the result is discarded and the error is a general one (an
# OSError without errno), even though the call itself succeeded: a listener in
# the wrong directory or a stream to the wrong server must not reach the
# caller as if it were at the path they gave. The bind's socket file is left
# where it went (where that is is not known, so nothing is removed); the
# connected stream is closed, which its server sees as end of file. Runs in a
# child process because it changes the cwd; see the scenario for the steps.
def test_a_cwd_changed_during_a_relative_bind_or_connect_is_a_general_error(tmp: TmpDirs) -> None:
    t = tmp.new()
    one = t + b"/one"
    two = t + b"/two"
    p = nest_tail(one, 90)
    sub = p[len(one) + 1 :]
    os.makedirs(two + b"/" + os.path.dirname(sub))
    run_child("cwd_changed", one, {"PATH": p, "OTHER": two})
    assert not is_socket(p)
    assert is_socket(two + b"/" + sub)


# A failure on the relative route is the kernel's error for the path (ENOENT
# for a missing socket, EADDRINUSE for an existing one, ECONNREFUSED for a
# socket nobody listens on), and nothing after the relative route is tried: the
# place for the symlink stays untouched though the symlink route is on (and
# the thread-cwd route too). Runs in a child whose cwd is near the socket.
def test_a_failure_on_the_relative_route_is_not_retried(tmp: TmpDirs) -> None:
    t = tmp.new()
    base = tmp.new()
    p = nest_tail(t, DIRECT_MAX)
    # The same length as p, so its relative spelling fits too.
    stale = with_name(p, b"x.sock")
    stale_socket(stale)
    mark_untouched(base)
    out = run_child("relative_failures", t, {"PATH": p, "BASE": base, "STALE": stale})
    assert out == {
        "no-socket": str(errno.ENOENT),
        "in-use": str(errno.EADDRINUSE),
        "stale": str(errno.ECONNREFUSED),
    }
    assert_untouched(base)


# ----- the thread-cwd route (Linux, Apple) -----


# After the symlink route, the file name is bound / connected from a throwaway
# thread whose own cwd is the socket's directory. Forced with a path far from
# the cwd and no place for a symlink, it gets every check of a route: the
# socket at the given path, data both ways, the blocking and nonblocking
# connect (the thread-cwd route is tried for `connect` too, unlike the fork
# route: it has no side effect), close-on-exec, the peer is this process (the
# server's SO_PEERCRED / LOCAL_PEERPID names the caller: the thread belongs to
# it), the process cwd unchanged, the path reported as the address (the kernel
# holds the file name alone, which names a file in whatever the process cwd
# is), asyncio serving and cleaning up as for the standard's, and a
# nonblocking connect fails at once on a full backlog.
@needs_thread_cwd
def test_the_thread_cwd_route_binds_and_connects_from_this_process(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 600)
    assert_far_from_cwd(p)
    f = forced("thread-cwd", p, posix_cfg(thread_cwd=True), Route.THREAD_CWD)
    check_route(f)
    check_full_backlog(f)


# The file name goes into a sockaddr of its own, so the thread-cwd route
# reaches names up to what a sockaddr carries, whatever the directory, and one
# byte more is ENAMETOOLONG with nothing created (no route left):
#
# * a bind takes names up to DIRECT_MAX, on every system (Apple included: a
#   bind never takes the widened form, whose address would not fit sun_path);
# * a connect takes names up to DIRECT_MAX too, except on Apple, where it takes
#   names up to 253 bytes in the widened form; with the widened form refused
#   (wide=False) the limit there is DIRECT_MAX.
#
# Names of 6, 100 and DIRECT_MAX bytes are bound and connected to through the
# route. On Apple names of DIRECT_MAX + 1, 200 and 253 bytes are ENAMETOOLONG
# for the bind with nothing created, and a connect reaches a listener at such a
# path (bound under a short name and renamed, the socket file being the same
# inode) through the route.
@needs_thread_cwd
def test_the_thread_cwd_route_takes_names_up_to_what_a_sockaddr_carries(tmp: TmpDirs) -> None:
    t = tmp.new()
    cases = [(posix_cfg(thread_cwd=True), THREAD_CONNECT_NAME_MAX)]
    if IS_APPLE:
        cases.append((posix_cfg(thread_cwd=True, wide=False), DIRECT_MAX))
    for i, (c, longest) in enumerate(cases):
        for n in (6, 100, DIRECT_MAX):
            p = nest(t, 600, (f"{i}" + "n" * (n - 1)).encode())
            listener, r = _route.run_long(BIND, p, c)
            with listener:
                assert r is Route.THREAD_CWD, n
                assert is_socket(p)
                for req in (CONNECT, CONNECT_NONBLOCKING):
                    client, r = _route.run_long(req, p, c)
                    with client:
                        assert r is Route.THREAD_CWD, (n, req)
                        accept_ping(listener, client)
        for n in [m for m in (DIRECT_MAX + 1, 200, WIDE_PATH_MAX) if m <= longest]:
            p = nest(t, 600, (f"{i}" + "m" * (n - 1)).encode())
            before = listing(parent(p))
            with raises_errno(errno.ENAMETOOLONG):
                _route.run_long(BIND, p, c)
            assert listing(parent(p)) == before
            with listener_at(p) as listener:
                for req in (CONNECT, CONNECT_NONBLOCKING):
                    client, r = _route.run_long(req, p, c)
                    with client:
                        assert r is Route.THREAD_CWD, (n, req)
                        accept_ping(listener, client)
        check_unreachable(nest(t, 600, (f"{i}" + "o" * longest).encode()), c)


# A failure on the thread-cwd route is the kernel's error and is returned as
# it is (EADDRINUSE for an existing socket, ENOENT for a missing one,
# ECONNREFUSED for a socket nobody listens on), not taken as "the route cannot
# be used" (which would end in ENAMETOOLONG).
@needs_thread_cwd
def test_a_failure_on_the_thread_cwd_route_is_the_kernels(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 600)
    c = posix_cfg(thread_cwd=True)
    with pytest.raises(FileNotFoundError):
        _route.run_long(CONNECT, p, c)
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is Route.THREAD_CWD
        with raises_errno(errno.EADDRINUSE):
            _route.run_long(BIND, p, c)
    stale = with_name(p, b"stale.sock")
    stale_socket(stale)
    for req in (CONNECT, CONNECT_NONBLOCKING):
        with pytest.raises(ConnectionRefusedError):
            _route.run_long(req, stale, c)


# The thread-cwd route changes the cwd of its own thread only: threads that
# keep checking the process cwd (getcwd and the identity of ".") the whole
# time many binds and connects run through it never see a change.
@needs_thread_cwd
def test_the_thread_cwd_route_never_changes_the_process_cwd(tmp: TmpDirs) -> None:
    cwd, cwd_id = os.getcwd(), dir_id(".")
    c = posix_cfg(thread_cwd=True)
    stop = threading.Event()
    checks = [0]
    errors: list[BaseException] = []

    def watch() -> None:
        while not stop.is_set():
            if os.getcwd() != cwd or dir_id(".") != cwd_id:
                errors.append(AssertionError("the process cwd changed"))
                return
            checks[0] += 1

    def work(i: int) -> None:
        try:
            t = tmp.new()
            for round_ in range(20):
                p = nest(t, 600, f"{i}-{round_}.sock".encode())
                listener, r = _route.run_long(BIND, p, c)
                with listener:
                    assert r is Route.THREAD_CWD
                    client, r = _route.run_long(CONNECT, p, c)
                    with client:
                        assert r is Route.THREAD_CWD
                        server, _ = listener.accept()
                        with server:
                            ping(server, client)
        except BaseException as e:
            errors.append(e)

    watchers = [threading.Thread(target=watch) for _ in range(2)]
    workers = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for th in watchers + workers:
        th.start()
    for th in workers:
        th.join()
    stop.set()
    for th in watchers:
        th.join()
    assert errors == []
    assert checks[0] > 0


# Where the thread-cwd route cannot be used, the order goes on to the next
# place (the fork route's, ENAMETOOLONG here) with nothing created:
#
# * the system refuses a cwd of the thread's own (Linux: a seccomp filter is
#   on, as under docker's default, or unshare(CLONE_FS) is refused; Apple:
#   pthread_fchdir_np missing or failing) — on such a system the real
#   refusal is what is checked, elsewhere it is stood in for at the boundary
#   (enter_dir_in_this_thread answering False);
# * a thread cannot be started;
# * the configuration turns the route off.
@pytest.mark.skipif(not _sys.HAS_THREAD_CWD, reason="no thread-cwd route on this system")
def test_where_the_thread_cwd_route_cannot_be_used_the_order_goes_on(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = tmp.new()
    c = posix_cfg(thread_cwd=True)
    if not thread_cwd_usable():
        check_unreachable(nest(t, 600, b"refused.sock"), c)
        return

    check_unreachable(nest(t, 600, b"off.sock"), posix_cfg(thread_cwd=False))

    with monkeypatch.context() as m:
        m.setattr(_route, "enter_dir_in_this_thread", lambda _fd: False)
        check_unreachable(nest(t, 600, b"refused.sock"), c)

    class NoThread:
        def __init__(self, **_: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    with monkeypatch.context() as m:
        m.setattr("sockat._route.threading", types.SimpleNamespace(Thread=NoThread, Lock=threading.Lock))
        check_unreachable(nest(t, 600, b"nothread.sock"), c)


# The thread-cwd route opens the directory as the fork route would: Linux
# O_PATH, Apple O_SEARCH, both needing only search permission, like a plain
# bind / connect by path. A directory of mode 0300 (write + search, no read)
# cannot be opened O_RDONLY, yet the route binds and connects in it. Root
# bypasses permission checks, so as root the test cannot tell anything.
@needs_thread_cwd
@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the permission under test")
def test_the_thread_cwd_route_needs_no_read_permission_on_the_directory(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 600)
    os.chmod(parent(p), 0o300)
    with pytest.raises(PermissionError):
        os.open(parent(p), os.O_RDONLY)
    c = posix_cfg(thread_cwd=True)
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is Route.THREAD_CWD
        assert is_socket(p)
        client, r = _route.run_long(CONNECT, p, c)
        with client:
            assert r is Route.THREAD_CWD
            accept_ping(listener, client)


# ----- the fork route's place -----


# This implementation has no fork route (a child forked from Python runs
# Python, which is not async-signal-safe). A path only the fork route would
# reach (600 bytes, so neither relative nor widened; no usable place for a
# symlink: none, one that does not exist, one too long for a spelling through
# it; the thread-cwd route off) fails with ENAMETOOLONG, the error of a plain
# bind / connect of a path too long for sun_path, for the bind and both
# connects, and nothing happens: nothing appears next to the socket, no
# connection reaches a listener there, nothing is created in the place. The
# other routes are not affected: with a short place, or the thread-cwd route
# where it can be used, the same path is reached.
def test_where_only_the_fork_route_reaches_everything_is_enametoolong(tmp: TmpDirs) -> None:
    t = tmp.new()
    p = nest(t, 600)
    assert_far_from_cwd(p)
    long_place = nest(tmp.new(), 250, b"place")
    os.mkdir(long_place)
    for bases in ([], [NO_PLACE], [long_place]):
        mark_untouched(long_place)
        check_unreachable(p, posix_cfg(bases), [long_place])
    short_place = tmp.new()
    listener, r = _route.run_long(BIND, with_name(p, b"q.sock"), posix_cfg([short_place]))
    listener.close()
    assert r is Route.SYMLINK_TO_DIR
    if thread_cwd_usable():
        listener, r = _route.run_long(BIND, with_name(p, b"r.sock"), posix_cfg(thread_cwd=True))
        listener.close()
        assert r is Route.THREAD_CWD


# A real failure stays what it is: a missing directory is ENOENT, not
# ENAMETOOLONG, for the bind and both connects, whatever the file name's
# length (6 bytes, and 254, longer than any sockaddr carries) and whether the
# thread-cwd route is on: the directory is opened before the thread-cwd route
# and the fork route's place, so its error comes before any length is judged.
def test_a_missing_directory_is_enoent_before_any_length_is_judged(tmp: TmpDirs) -> None:
    t = tmp.new()
    directory = parent(nest(t, 600))
    for n in (6, 254):
        missing = directory + b"/nope/" + b"n" * n
        for thread_cwd in (False, True):
            for req in (BIND, CONNECT, CONNECT_NONBLOCKING):
                with pytest.raises(FileNotFoundError):
                    _route.run_long(req, missing, posix_cfg(thread_cwd=thread_cwd))
    assert listing(directory) == []


# The bindat route (FreeBSD) opens the directory before it builds the
# sockaddr of the file name, so a missing directory is ENOENT even when the
# file name is too long for a sockaddr (SPEC: the directory is opened first,
# before any length is judged). at_op raises both before bindat(2) would be
# called, so the order is checked on any system.
def test_the_bindat_route_judges_the_directory_before_the_file_name(tmp: TmpDirs) -> None:
    missing = tmp.new() + b"/nope"
    for req in (BIND, CONNECT, CONNECT_NONBLOCKING):
        with pytest.raises(FileNotFoundError):
            _route.at_op(req, missing, b"n" * 104)


# When the caller stops waiting for the thread-cwd thread (an exception such
# as KeyboardInterrupt out of join), the socket may still be in use by the
# thread, so the caller must not close it; the thread closes it when it ends
# instead. Otherwise the socket's fd would stay open with nobody owning it.
# The interruption is stood in for by a join that raises once. The test keeps
# its own reference to the socket the route made (so garbage collection cannot
# close it in the route's place) and checks it is closed once the thread has
# ended.
@needs_thread_cwd
def test_an_interrupted_wait_for_the_thread_cwd_thread_leaves_no_fd_behind(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = nest(tmp.new(), 600)
    listener, r = _route.run_long(BIND, p, posix_cfg(thread_cwd=True))
    with listener:
        assert r is Route.THREAD_CWD
        made: list[socket.socket] = []
        threads: list[threading.Thread] = []
        real_new_socket = _route.new_socket
        real_join = threading.Thread.join

        def new_socket(req: _route.Request) -> socket.socket:
            sock = real_new_socket(req)
            made.append(sock)
            return sock

        def interrupted_join(self: threading.Thread, timeout: float | None = None) -> None:
            threads.append(self)
            raise KeyboardInterrupt

        monkeypatch.setattr(_route, "new_socket", new_socket)
        monkeypatch.setattr(threading.Thread, "join", interrupted_join)
        dirfd = os.open(parent(p), os.O_RDONLY)
        try:
            with pytest.raises(KeyboardInterrupt):
                _route.thread_cwd_op(CONNECT, dirfd, b"s.sock", False)
            monkeypatch.setattr(threading.Thread, "join", real_join)
            for t in threads:
                t.join()
            assert len(made) == 1
            assert made[0].fileno() == -1
        finally:
            os.close(dirfd)
            for sock in made:
                sock.close()


# ----- opening the directory -----


# A kernel that does not take O_SEARCH on a directory answers EISDIR (as macOS
# does for O_EXEC alone) or EINVAL. The directory must then be opened O_RDONLY
# instead, rather than failing on a directory the route could use. The running
# kernel takes O_SEARCH, so the refusal is stood in for by the function handed
# to search_or_read_only. Checked:
#
# * EISDIR and EINVAL give an fd of that directory, opened O_RDONLY;
# * any other error from the O_SEARCH open is the real outcome and is raised
#   without trying O_RDONLY: an EACCES for a readable directory stays EACCES
#   (falling back would hide a real permission failure), as does ENOENT.
def test_a_kernel_refusing_o_search_falls_back_to_o_rdonly(tmp: TmpDirs) -> None:
    d = tmp.new()

    def refused(code: int) -> Callable[[], int]:
        def search() -> int:
            raise OSError(code, os.strerror(code))

        return search

    for code in (errno.EISDIR, errno.EINVAL):
        fd = _route.search_or_read_only(d, refused(code))
        try:
            st = os.fstat(fd)
            assert (st.st_dev, st.st_ino) == dir_id(d)
            assert fcntl.fcntl(fd, fcntl.F_GETFL) & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR) == os.O_RDONLY
        finally:
            os.close(fd)
    for code in (errno.EACCES, errno.ENOENT):
        with raises_errno(code):
            _route.search_or_read_only(d, refused(code))


# The fallback really is O_RDONLY: in a directory of mode 0300 (write +
# search, no read), which O_SEARCH (Apple) and O_PATH (Linux) open, the
# fallback fails with EACCES, and that is the error returned, not swallowed.
@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the permission under test")
@pytest.mark.skipif(not _sys.HAS_THREAD_CWD, reason="O_SEARCH / O_PATH are the Apple / Linux ways of opening")
def test_the_o_rdonly_fallback_needs_read_permission(tmp: TmpDirs) -> None:
    locked = tmp.new() + b"/locked"
    os.mkdir(locked, 0o300)
    os.chmod(locked, 0o300)
    os.close(_route.open_dir(locked))

    def refused() -> int:
        raise OSError(errno.EISDIR, os.strerror(errno.EISDIR))

    with pytest.raises(PermissionError):
        _route.search_or_read_only(locked, refused)
