"""The routes of a connect to a long path (hub SPEC "connect の順",
SPEC-routes "経路"): each forced, the order of this system, nothing left
behind and no wait, failures that are the outcome, the relative route's
check of the cwd, the widened sockaddr (Apple) and the nonblocking connect
on a full backlog."""

from __future__ import annotations

import errno
import os
import socket

import pytest
from _support import (
    FULL_BACKLOG,
    HAS_PROC,
    NO_PROC,
    THREAD_CWD,
    THREAD_CWD_DIRECT_IN_CONNECT_ROW,
    TmpDirs,
    accept_ping,
    at_once,
    connect_in,
    env_at,
    kernel_peername,
    listener_at,
    listing,
    long_path,
    nest,
    raises_errno,
    run_child,
    stale_socket,
    tree,
)

from sockat import _calls
from sockat._routes import Call, Relative
from sockat._spelling import Trail
from sockat._sys import DIRECT_MAX, IS_APPLE, IS_FREEBSD, IS_LINUX, Op

ROUTES_HERE: list[str] = ["relative", "link-file"]
if HAS_PROC:
    ROUTES_HERE.append("proc-direct")
if IS_APPLE:
    ROUTES_HERE.append("widened")
if IS_FREEBSD:
    ROUTES_HERE.append("at-direct")
if THREAD_CWD:
    ROUTES_HERE.append("thread-cwd-direct")


def expected_order(proc_ok: bool) -> list[str]:
    if IS_LINUX:
        first = ["proc-direct"] if proc_ok else ["relative"]
    elif IS_APPLE:
        first = ["relative", "widened"]
    elif IS_FREEBSD:
        first = ["at-direct"]
    else:
        first = ["relative"]
    # thread-cwd-direct only where a thread can be given a cwd of its own
    # (SPEC: a route the system has no means for is not in its row).
    return [*first, *(["thread-cwd-direct"] if THREAD_CWD_DIRECT_IN_CONNECT_ROW else []), "link-file"]


# Each route of this system, forced alone, reaches the listener at a long
# path (the cwd set where relative fits), stream and blocking / nonblocking,
# and leaves nothing in B or next to the socket.
@pytest.mark.parametrize("route", ROUTES_HERE)
@pytest.mark.parametrize("nonblocking", [False, True])
def test_each_route_reaches_the_socket(tmp: TmpDirs, route: str, nonblocking: bool) -> None:
    place = tmp.new()
    p = long_path(tmp.new())
    with listener_at(p) as listener:
        before = tree(place), listing(os.path.dirname(p))
        code = """
            from _support import env_at, connect_in, accept_ping
            import socket
            env = env_at(os.fsencode(ARGS["place"]))
            sock, route = connect_in(env, os.fsencode(ARGS["path"]), nonblocking=ARGS["nb"], order=(ARGS["route"],))
            sock.sendall(b"x")
            report(route=route, blocking=sock.getblocking())
            """
        # relative needs a cwd near the socket: in a child, so the test's cwd
        # stays as it is.
        cwd = os.path.dirname(p) if route in ("relative",) else b"/"
        out = run_child(
            code,
            cwd=cwd,
            args={"place": os.fsdecode(place), "path": os.fsdecode(p), "nb": nonblocking, "route": route},
        )
        assert out["route"] == route
        assert out["blocking"] is not nonblocking
        conn, _ = listener.accept()
        with conn:
            assert conn.recv(1) == b"x"
        assert listing(os.path.dirname(p)) == before[1]
        if route == "link-file":
            # The connect made B (kept, as B is), and nothing else: no D, the
            # E right in B removed.
            assert tree(place) == [os.fsdecode(b"sockat-%d" % os.geteuid()) + "/"]
        else:
            assert tree(place) == before[0] == []


# The default order of this system, with what it skips: the routes that
# need no write first, then thread-cwd-direct, then link-file.
def test_the_order_of_this_system(tmp: TmpDirs) -> None:
    env = env_at(tmp.new())
    p = long_path(tmp.new(), name=b"n" * (DIRECT_MAX - 7))
    with listener_at(p) as listener:
        trail = Trail()
        sock, route = _calls.connect(p, call=Call(Op.CONNECT), env=env, trail=trail)
        with sock:
            accept_ping(listener, sock)
        order = expected_order(HAS_PROC)
        tried = [s.name for s in trail.skipped]
        assert tried[0] == "standard"
        assert [*tried[1:], route] == order[: len(tried)]


# The default connect leaves nothing in the process or in B: going all the
# way to link-file (no /proc, no thread cwd, the cwd far away), B holds no D
# and no E afterwards, and the nonblocking connect returns at once.
def test_the_default_connect_leaves_nothing(tmp: TmpDirs) -> None:
    place = tmp.new()
    env = env_at(place, proc=NO_PROC, thread_cwd=False, wide=False)
    # A file name past every sockaddr, so that no route before link-file
    # reaches it on any system (FreeBSD's at-direct carries 103 bytes).
    p = long_path(tmp.new(), name=b"n" * 110, total=230)
    with listener_at(p) as listener:
        for nonblocking in (False, True):
            sock, route = at_once(lambda nb=nonblocking: connect_in(env, p, nonblocking=nb))  # type: ignore[misc]
            with sock:
                assert route == "link-file"
                accept_ping(listener, sock)
        b = env.auto_base()
        assert b is not None
        assert os.listdir(b.real) == []
        assert b._d is None


# A connect's real failure is the outcome, raised with the route that failed;
# no later route is tried (nothing appears in B): ENOENT with no socket file,
# ECONNREFUSED with one nobody listens on. Run from the socket's directory
# (in a child), so that the first route of every system's row can be used
# (relative fits there; proc-direct, at-direct need no cwd) and link-file,
# last in every row, comes after it.
@pytest.mark.parametrize(("stale", "code"), [(False, errno.ENOENT), (True, errno.ECONNREFUSED)])
def test_a_connect_failure_is_not_tried_again(tmp: TmpDirs, stale: bool, code: int) -> None:
    place = tmp.new()
    p = long_path(tmp.new())
    if stale:
        stale_socket(p)
    out = run_child(
        """
        import errno
        from _support import env_at
        from sockat import _calls
        from sockat._routes import Call
        from sockat._spelling import Trail
        from sockat._sys import Op
        trail = Trail()
        try:
            env = env_at(os.fsencode(ARGS["place"]))
            _calls.connect(os.fsencode(ARGS["p"]), call=Call(Op.CONNECT), env=env, trail=trail)
            report(errno=None)
        except OSError as e:
            report(errno=e.errno, trying=trail.trying)
        """,
        cwd=os.path.dirname(p),
        args={"p": os.fsdecode(p), "place": os.fsdecode(place)},
    )
    assert out["errno"] == code
    assert out["trying"] == expected_order(HAS_PROC)[0]
    assert listing(place) == []


# link-file's failure is the outcome too, and its E is removed.
def test_link_file_failure_removes_its_e(tmp: TmpDirs) -> None:
    place = tmp.new()
    env = env_at(place)
    p = long_path(tmp.new())
    with raises_errno(errno.ENOENT):
        connect_in(env, p, order=("link-file",))
    b = env.auto_base()
    assert b is not None
    assert os.listdir(b.real) == []


# relative is used only while the cwd stays: if it changes between making
# the spelling and the connect, the result is thrown away and a general error
# (no errno) raised; it never reaches another place.
def test_relative_with_the_cwd_changed_is_a_general_error(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    other = tmp.new()
    with listener_at(p):
        out = run_child(
            """
            from sockat._routes import Relative
            from sockat._errors import CwdChanged
            path = os.fsencode(ARGS["path"])
            rel = Relative.new(path, os.path.dirname(path))
            def connect(spelling):
                os.chdir(ARGS["other"])
                s = socket.socket(socket.AF_UNIX)
                s.connect(spelling)
                return s
            try:
                rel.run(connect)
                report(error="none")
            except CwdChanged as e:
                report(error="CwdChanged", errno=e.errno)
            """,
            cwd=os.path.dirname(p),
            args={"path": os.fsdecode(p), "other": os.fsdecode(other)},
        )
    assert out["error"] == "CwdChanged"
    assert out["errno"] is None


# relative is not used when its directory turns out not to be the socket's
# (a $PWD naming another directory is not trusted).
def test_relative_checks_its_directory(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    out = run_child(
        """
        from sockat._routes import Relative
        path = os.fsencode(ARGS["path"])
        rel = Relative.new(path, os.path.dirname(path))
        report(rel=None if rel is None else os.fsdecode(rel.path))
        """,
        cwd=os.path.dirname(p),
        env={"PWD": "/"},
        args={"path": os.fsdecode(p)},
    )
    assert out["rel"] == "s.sock"


# Apple: a connect reaches a path of 253 bytes (the widened sockaddr's
# longest) from a cwd far away; 254 bytes takes another route. A bind never
# uses the widened sockaddr.
@pytest.mark.skipif(not IS_APPLE, reason="the widened sockaddr is Apple's")
def test_widened_reaches_253_bytes(tmp: TmpDirs) -> None:
    env = env_at(tmp.new(), thread_cwd=False)
    for total, want in ((253, "widened"), (254, "link-file")):
        p = nest(tmp.new(), total)
        with listener_at(p) as listener:
            sock, route = connect_in(env, p)
            with sock:
                assert route == want
                accept_ping(listener, sock)
        trail = Trail()
        os.unlink(p)
        done, _ = _calls.bind(p, env=env, trail=trail)
        with done.sock:
            assert done.route != "widened"
            assert "widened" not in [s.name for s in trail.skipped]


# A nonblocking connect to a full backlog fails at once (Linux EAGAIN,
# macOS ECONNREFUSED), on every route of this system that does not need the
# cwd moved near the socket (relative is the standard's own connect on a
# relative spelling), rather than wait.
@pytest.mark.skipif(not (IS_LINUX or IS_APPLE), reason="confirmed on Linux and macOS only")
@pytest.mark.parametrize("route", [r for r in ROUTES_HERE if r != "relative"])
def test_a_nonblocking_connect_to_a_full_backlog_fails_at_once(tmp: TmpDirs, route: str) -> None:
    env = env_at(tmp.new())
    p = long_path(tmp.new())
    with listener_at(p) as listener:
        listener.listen(1)
        queued: list[socket.socket] = []
        try:
            full: int | None = None
            for _ in range(16):
                try:
                    sock, got = at_once(lambda: connect_in(env, p, nonblocking=True, order=(route,)))
                except OSError as e:
                    full = e.errno
                    break
                assert got == route
                queued.append(sock)
            assert full == FULL_BACKLOG, "the backlog never filled" if full is None else errno.errorcode[full]
            assert queued
        finally:
            for s in queued:
                s.close()


# A connected stream reports the path given as its peer, where the kernel
# reports the spelling the route connected through.
def test_a_connected_stream_reports_the_path(tmp: TmpDirs) -> None:
    env = env_at(tmp.new())
    p = long_path(tmp.new())
    with listener_at(p) as listener:
        sock, route = connect_in(env, p, order=("link-file",))
        with sock:
            assert sock.getpeername() == os.fsdecode(p)
            assert kernel_peername(sock) != os.fsdecode(p)
            accept_ping(listener, sock)
        assert route == "link-file"


# Options.base gives link-file its B too: the connect goes through an E right
# in it, removed afterwards.
def test_options_base_for_a_connect(tmp: TmpDirs) -> None:
    given = tmp.new() + b"/b"
    env = env_at(b"/nonexistent/place")
    p = long_path(tmp.new())
    with listener_at(p) as listener:
        sock, route = _calls.connect(
            p, call=Call(Op.CONNECT), env=env, opts=_calls.Opts(base=given), order=("link-file",)
        )
        with sock:
            assert route == "link-file"
            accept_ping(listener, sock)
    assert os.listdir(given) == []


# Relative is not made for a relative path (the routes of a connect take an
# absolute one; a relative long path reaches by the others).
def test_relative_needs_an_absolute_path() -> None:
    assert Relative.new(b"a/" + b"x" * 200 + b"/s.sock", b"a/" + b"x" * 200) is None


# Only the routes that need the socket's directory's fd open it
# (SPEC-routes): relative, widened and link-file do not.
def test_only_routes_needing_the_directory_open_it(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    from sockat import _place, _routes

    opened: list[bytes] = []
    real_open = _place.open_dir

    def counting(directory: bytes, *, nofollow: bool = False, dir_fd: int | None = None) -> int:
        opened.append(directory)
        return real_open(directory, nofollow=nofollow, dir_fd=dir_fd)

    monkeypatch.setattr(_routes, "open_dir", counting)
    env = env_at(tmp.new())
    p = long_path(tmp.new())
    with listener_at(p) as listener:
        sock, route = connect_in(env, p, order=("link-file",))
        with sock:
            accept_ping(listener, sock)
    assert route == "link-file"
    assert os.path.dirname(p) not in opened
