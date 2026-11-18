"""Scenarios run in a child process by :func:`_support.run_child`, for the
tests that need a cwd, ``$PWD`` or ``$TMPDIR`` of their own (process-wide
state the test process must not change).

Run as ``python _children.py <scenario>`` with the cwd and environment the
test chose; arguments come as ``SOCKAT_ARG_<KEY>`` environment variables. A
scenario asserts what it can see from inside and reports the rest as one
JSON line (:data:`_support.RESULT_MARK`) for the test to check; a failed
assertion exits non-zero, which fails the test.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import resource
import socket
import sys
from collections.abc import Callable

from _support import (
    CHECKS_FULL_BACKLOG,
    RESULT_MARK,
    Forced,
    accept_ping,
    assert_cleaned_up_as_asyncio_does,
    assert_untouched,
    check_full_backlog,
    check_route,
    forced,
    is_socket,
    nest,
    posix_cfg,
    raises_errno,
    routes,
    serve_asyncio,
    thread_cwd_usable,
    with_name,
)

import sockat
from sockat import _route
from sockat._route import BIND, CONNECT, Config, Request, Route
from sockat._sys import DIRECT_MAX, WIDE_PATH_MAX


def arg(key: str) -> bytes:
    return os.environb[b"SOCKAT_ARG_" + key.encode()]


def opt_arg(key: str) -> bytes | None:
    return os.environb.get(b"SOCKAT_ARG_" + key.encode())


def report(**values: str) -> None:
    print(RESULT_MARK + json.dumps(values), flush=True)


def make_stale_socket() -> None:
    """Bind a socket at PATH and exit without anyone listening on it any
    more."""
    _route.run(BIND, arg("PATH"))[0].close()
    report()


def bind_report() -> None:
    """Bind PATH through the POSIX order (no place for a symlink, /proc made
    to look absent on Linux, at PROC when given, no thread-cwd route) and
    report the route taken and the relative spelling (``-`` when there is
    none)."""
    p = arg("PATH")
    directory, _ = os.path.split(p)
    rel = _route.Relative.new(p, directory)
    spelling = os.fsdecode(rel.path) if rel is not None else "-"
    proc = opt_arg("PROC")
    cfg = posix_cfg(proc=proc) if proc is not None else posix_cfg()
    listener, route = _route.run_long(BIND, p, cfg)
    listener.close()
    report(route=route.value, rel=spelling)


def bind_from_env() -> None:
    """Bind PATH with the configuration read from the environment
    (``Config.from_env``: ``$TMPDIR``, then ``/tmp``), /proc made to look
    absent on Linux, and report the route."""
    base = Config.from_env()
    cfg = Config(tmp_bases=base.tmp_bases, wide=base.wide, proc_self_fd=posix_cfg().proc_self_fd, thread_cwd=False)
    listener, route = _route.run_long(BIND, arg("PATH"), cfg)
    listener.close()
    report(route=route.value, bases=json.dumps([os.fsdecode(b) for b in cfg.tmp_bases]))


def connect_report() -> None:
    """Connect to PATH (absolute, or relative to the cwd) on Linux with the
    real /proc and no thread-cwd route, through BASE as the only place for
    the symlink, or through the places read from the environment
    (``Config.from_env``: ``$TMPDIR``, then ``/tmp``) when BASE is not given,
    with the umask UMASK (octal) when given, and report the route and the
    places."""
    umask = opt_arg("UMASK")
    if umask is not None:
        os.umask(int(umask, 8))
    base = opt_arg("BASE")
    bases = (base,) if base is not None else Config.from_env().tmp_bases
    cfg = Config(tmp_bases=bases, proc_self_fd=b"/proc/self/fd", thread_cwd=False)
    client, route = _route.run_long(CONNECT, arg("PATH"), cfg)
    client.close()
    report(route=route.value, bases=json.dumps([os.fsdecode(b) for b in cfg.tmp_bases]))


def relative_routes() -> None:
    """Every check of a forced route (:func:`_support.check_route`, and
    :func:`_support.check_full_backlog`) on the relative route, with PATH
    below the cwd so that its relative spelling fits the plain sockaddr; on
    Apple also, for the connects, on the widened sockaddr with the relative
    spelling (WPATH, whose relative spelling fits only the widened one). A
    bind never takes the widened form: WPATH is ENAMETOOLONG for the bind
    with the thread-cwd route off, nothing created, and with it on the bind
    goes by the file name from the thread-cwd route."""
    ways: list[Forced] = [forced("relative", arg("PATH"), posix_cfg(), Route.RELATIVE)]
    wpath = opt_arg("WPATH")
    if wpath is not None:
        rel = _route.Relative.new(wpath, os.path.dirname(wpath))
        assert rel is not None
        assert DIRECT_MAX < len(rel.path) <= WIDE_PATH_MAX, rel
        before = sorted(os.listdir(os.path.dirname(wpath)))
        with raises_errno(errno.ENAMETOOLONG):
            _route.run_long(BIND, wpath, posix_cfg())[0].close()
        assert sorted(os.listdir(os.path.dirname(wpath))) == before
        want = routes(Route.THREAD_CWD, Route.WIDE_RELATIVE)
        ways.append(forced("wide-relative", wpath, posix_cfg(thread_cwd=True), want))
    for f in ways:
        check_route(f)
        if CHECKS_FULL_BACKLOG:
            check_full_backlog(f)
    report(checked=",".join(f.what for f in ways))


def relative_failures() -> None:
    """On the relative route (PATH below the cwd), the kernel's errors come
    back as they are and nothing is tried after them: the place for the
    symlink (BASE, marked untouched by the test) stays untouched, though the
    symlink route and the thread-cwd route are both on."""
    p = arg("PATH")
    base = arg("BASE")
    stale = arg("STALE")
    cfg = posix_cfg([base], thread_cwd=True)
    for q in (p, stale):
        # The premise: each path's relative spelling fits the plain sockaddr,
        # so the relative route is the one that fails.
        rel = _route.Relative.new(q, os.path.dirname(q))
        assert rel is not None
        assert len(rel.path) <= DIRECT_MAX, rel
    errors: dict[str, str] = {}
    try:
        _route.run_long(CONNECT, p, cfg)
    except OSError as e:
        errors["no-socket"] = str(e.errno)
    listener, route = _route.run_long(BIND, p, cfg)
    assert route is Route.RELATIVE, route
    with listener:
        try:
            _route.run_long(BIND, p, cfg)
        except OSError as e:
            errors["in-use"] = str(e.errno)
    try:
        _route.run_long(CONNECT, stale, cfg)
    except OSError as e:
        errors["stale"] = str(e.errno)
    assert_untouched(base)
    report(**errors)


def relative_input() -> None:
    """A long path given relative to the cwd (REL) is bound and connected to
    in the directory it names below the cwd, through the given
    configuration (BASE as the only place for the symlink, or none;
    THREAD=1 turns the thread-cwd route on), and the routes are reported."""
    rel = arg("REL")
    base = opt_arg("BASE")
    cfg = posix_cfg([base] if base else [], thread_cwd=opt_arg("THREAD") == b"1")
    listener, bind_route = _route.run_long(BIND, rel, cfg)
    with listener:
        assert is_socket(rel)
        # Reported as given, relative, as the standard reports a short
        # relative path it bound or connected to.
        assert listener.getsockname() == os.fsdecode(rel)
        client, connect_route = _route.run_long(CONNECT, rel, cfg)
        with client:
            assert client.getpeername() == os.fsdecode(rel)
            accept_ping(listener, client)
    report(bind=bind_route.value, connect=connect_route.value)


UNRELATED = b"unrelated, not to be removed"
"""What the files that stand next to a socket's spelling hold
(:func:`asyncio_namesakes`)."""


def put_unrelated(path: bytes) -> None:
    with open(path, "wb") as f:
        f.write(UNRELATED)


def assert_unrelated(path: bytes, what: str) -> None:
    assert os.path.lexists(path), what
    with open(path, "rb") as f:
        assert f.read() == UNRELATED, what


def asyncio_namesakes() -> None:
    """A listener whose kernel address is a spelling relative to the cwd, used
    with asyncio as the standard's is (:func:`_support.serve_asyncio`, bound
    before the loop), while a file unrelated to the socket sits where that
    spelling names from the process cwd. asyncio (Python 3.13 and later)
    removes the file at the listener's address when the server closes, if it
    is still the inode it found there at the start: had it been handed the
    spelling, it would have found the unrelated file both times and removed
    it. It must be left alone, and the socket file at the path removed (or,
    before 3.13, left) as for the standard's. Two spellings:

    * the thread-cwd route (where the system allows it): the file name alone,
      THREAD (far from the cwd, file name ``s.sock``), with ``s.sock`` in the
      cwd unrelated;
    * the relative route: REL below the cwd, then the cwd changed to OTHER,
      which holds an unrelated file at REL's relative spelling, before the
      loop starts.

    Reports which of them ran."""
    checked: list[str] = []

    def public(path: bytes) -> Callable[[Request], socket.socket]:
        def connect(req: Request) -> socket.socket:
            return sockat.connect_nonblocking(path) if req.nonblocking else sockat.connect(path)

        return connect

    if thread_cwd_usable():
        p = arg("THREAD")
        name = os.path.basename(p)
        put_unrelated(name)
        listener, r = _route.run_long(BIND, p, posix_cfg(thread_cwd=True))
        assert r is Route.THREAD_CWD, r
        assert socket.socket.getsockname(listener) == os.fsdecode(name), "the premise: the kernel holds the name"
        # Checked however the exchange ended: the server is closed (and its
        # cleanup run) on the way out of a failure too.
        try:
            asyncio.run(serve_asyncio("thread-cwd", listener, public(p), os.fsdecode(p), served=True))
        finally:
            assert_unrelated(name, "the unrelated file in the cwd was removed")
        assert_cleaned_up_as_asyncio_does("thread-cwd", p)
        checked.append("thread-cwd")

    p = arg("REL")
    other = arg("OTHER")
    rel = _route.Relative.new(p, os.path.dirname(p))
    assert rel is not None
    assert len(rel.path) <= DIRECT_MAX, rel
    namesake = os.path.join(other, rel.path)
    os.makedirs(os.path.dirname(namesake))
    put_unrelated(namesake)
    listener, r = _route.run_long(BIND, p, posix_cfg())
    assert r is Route.RELATIVE, r
    assert os.fsencode(socket.socket.getsockname(listener)) == rel.path, "the premise: the kernel holds the spelling"
    os.chdir(other)
    try:
        asyncio.run(serve_asyncio("relative", listener, public(p), os.fsdecode(p), served=True))
    finally:
        assert_unrelated(namesake, "the unrelated file at the spelling from the new cwd was removed")
    assert_cleaned_up_as_asyncio_does("relative", p)
    checked.append("relative")
    report(checked=",".join(checked))


def cwd_changed() -> None:
    """The relative spelling is resolved against the cwd at the moment of the
    bind / connect, and the cwd is shared by every thread, so another thread
    that changes it in between sends the call to another directory. When the
    cwd is still changed afterwards, ``Relative.run`` must notice and turn
    the result into an error even though the call itself succeeded.

    The change is made from inside the call (the function chdirs before it
    binds / connects), standing in for another thread. PATH is below the cwd
    (ONE); OTHER has the same subtree, so the call succeeds there."""
    p = arg("PATH")
    one = os.getcwdb()
    two = arg("OTHER")
    directory, _ = os.path.split(p)
    rel = _route.Relative.new(p, directory)
    assert rel is not None
    assert b".." not in rel.path.split(b"/"), rel.path

    def chdir_then(f: Callable[[bytes], socket.socket]) -> Callable[[bytes], socket.socket]:
        def call(q: bytes) -> socket.socket:
            os.chdir(two)
            return f(q)

        return call

    try:
        rel.run(chdir_then(lambda q: _route.plain_op(BIND, q)))
    except OSError as e:
        bind_error = e
    else:
        raise AssertionError("a bind into another directory was returned")
    assert type(bind_error) is _route.CwdChanged, repr(bind_error)
    assert bind_error.errno is None, repr(bind_error)
    # The bind went to the other directory and that socket file is left there
    # (where it went is not known to Relative.run, so it must not remove
    # anything); nothing is at the given path.
    assert is_socket(os.path.join(two, rel.path))
    assert not is_socket(p)

    # A listener in the other directory, at the same relative spelling.
    c = with_name(p, b"c.sock")
    c_rel = with_name(rel.path, b"c.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(c_rel)
    listener.listen()
    os.chdir(one)
    rel = _route.Relative.new(c, directory)
    assert rel is not None
    assert rel.path == c_rel
    try:
        rel.run(chdir_then(lambda q: _route.plain_op(CONNECT, q)))
    except OSError as e:
        connect_error = e
    else:
        raise AssertionError("a connect to another directory was returned")
    assert type(connect_error) is _route.CwdChanged, repr(connect_error)
    assert connect_error.errno is None
    # The stream that did connect to the listener in the other directory was
    # closed, which that listener sees as end of file.
    server, _ = listener.accept()
    with server, listener:
        assert server.recv(1) == b"", "the discarded client is closed"
    report(bind=repr(bind_error), connect=repr(connect_error))


def default_backlog() -> None:
    """Fill a standard listener at a short path under DIR and a sockat
    listener at a 400-byte path with nonblocking connects, and report how many
    got into each before it was full.

    Every queued connect holds an fd, so the soft limit of open files is first
    raised to the hard one: a default soft limit can run out before the
    backlog fills (OpenBSD's does), which would end the count early with
    EMFILE rather than at the backlog. The limit is process-wide, hence the
    child process."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = hard if hard != resource.RLIM_INFINITY else 10_240
    if soft != resource.RLIM_INFINITY and soft < want:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))

    def fill(path: bytes) -> int:
        queued: list[socket.socket] = []
        try:
            while len(queued) < 10_000:
                try:
                    queued.append(sockat.connect_nonblocking(path))
                except (BlockingIOError, ConnectionRefusedError):
                    return len(queued)
            raise AssertionError("the backlog never filled")
        finally:
            for c in queued:
                c.close()

    t = arg("DIR")
    short = t + b"/std.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as std:
        std.bind(short)
        std.listen()
        std_count = fill(short)
    long = nest(t, 400)
    with sockat.bind(long):
        sockat_count = fill(long)
    report(std=str(std_count), sockat=str(sockat_count))


SCENARIOS: dict[str, Callable[[], None]] = {
    f.__name__: f
    for f in (
        make_stale_socket,
        bind_report,
        bind_from_env,
        connect_report,
        relative_routes,
        relative_failures,
        relative_input,
        asyncio_namesakes,
        cwd_changed,
        default_backlog,
    )
}


if __name__ == "__main__":
    SCENARIOS[sys.argv[1]]()
