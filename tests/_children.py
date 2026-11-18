"""Scenarios run in a child process by :func:`_support.run_child`, for the
tests that need a cwd, ``$PWD``, ``$TMPDIR`` or a umask of their own
(process-wide state the test process must not change).

Run as ``python _children.py <scenario>`` with the cwd and environment the
test chose; arguments come as ``SOCKAT_ARG_<KEY>`` environment variables. A
scenario asserts what it can see from inside and reports the rest as one
JSON line (:data:`_support.RESULT_MARK`) for the test to check; a failed
assertion exits non-zero, which fails the test.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import re
import resource
import socket
import sys
from collections.abc import Callable

from _support import (
    CHECKS_FULL_BACKLOG,
    NO_PROC,
    PROC,
    RESULT_MARK,
    Forced,
    accept_ping,
    assert_cleaned_up_as_asyncio_does,
    assert_untouched,
    central_cfg,
    central_route_here,
    check_full_backlog,
    check_route,
    fallback_cfg,
    forced,
    is_socket,
    nest,
    routes,
    serve_asyncio,
)

import sockat
from sockat import _route
from sockat._route import BIND, CONNECT, CONNECT_NONBLOCKING, SOCKET_DIR, Config, OwnDirPlace, Request, Route
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


def means_cfg(means: str, own: tuple[OwnDirPlace, ...]) -> tuple[Config, Route]:
    """The configuration that forces one means of the central route (as
    ``test_central.means_here`` names them), with ``own`` as the places for
    D, and the route it takes."""
    if means == "thread-cwd":
        return central_cfg(own, proc=NO_PROC), Route.THREAD_CWD
    if means == "proc":
        return central_cfg(own, thread_cwd=False), Route.PROC_OWN_DIR
    assert means == "at", means
    return central_cfg(own), Route.AT


def connect_report() -> None:
    """Connect to PATH (absolute, or relative to the cwd) by the fallbacks
    alone (the thread's own cwd off; OWN as the only place for D when given,
    none otherwise), through BASE as the only place for the symlink, or the
    places read from the environment (``Config.from_env``: ``$TMPDIR``, then
    ``/tmp``) when BASE is not given; on Linux the real /proc when PROC is
    ``1``, none otherwise. Report the route and the places."""
    base = opt_arg("BASE")
    links = (base,) if base is not None else Config.from_env().link_places
    proc = PROC if opt_arg("PROC") == b"1" else NO_PROC
    own = opt_arg("OWN")
    cfg = dataclasses.replace(fallback_cfg(links, proc=proc), own_dir_places=(own,) if own is not None else ())
    client, route = _route.run_long(CONNECT, arg("PATH"), cfg)
    client.close()
    report(route=route.value, bases=json.dumps([os.fsdecode(b) for b in links]))


def relative_spelling() -> None:
    """Report the relative spelling of PATH from the cwd (``-`` for none)."""
    p = arg("PATH")
    rel = _route.Relative.new(p, os.path.dirname(p))
    report(rel=os.fsdecode(rel.path) if rel is not None else "-")


def bind_without_own_dir() -> None:
    """Bind PATH with no place for D (the fallbacks' configuration) and
    report the errno it fails with."""
    try:
        _route.run_long(BIND, arg("PATH"), fallback_cfg())[0].close()
    except OSError as e:
        report(errno=str(e.errno))
    else:
        raise AssertionError("the bind went through")


def relative_routes() -> None:
    """Every check of a forced route (:func:`_support.check_route`, and
    :func:`_support.check_full_backlog`) on the relative fallback of the
    connects, with PATH below the cwd so that its relative spelling fits the
    plain sockaddr; on Apple also on the widened sockaddr with the relative
    spelling (WPATH, whose relative spelling fits only the widened one). The
    listener is bound by the central route (a bind never takes the relative
    spelling)."""
    bind = central_cfg()
    bound = central_route_here()
    assert bound is not None
    ways: list[Forced] = [forced("relative", arg("PATH"), fallback_cfg(), routes(bound, Route.RELATIVE), bind_cfg=bind)]
    wpath = opt_arg("WPATH")
    if wpath is not None:
        rel = _route.Relative.new(wpath, os.path.dirname(wpath))
        assert rel is not None
        assert DIRECT_MAX < len(rel.path) <= WIDE_PATH_MAX, rel
        ways.append(forced("wide-relative", wpath, fallback_cfg(), routes(bound, Route.WIDE_RELATIVE), bind_cfg=bind))
    for f in ways:
        check_route(f)
        if CHECKS_FULL_BACKLOG:
            check_full_backlog(f)
    report(checked=",".join(f.what for f in ways))


def relative_failures() -> None:
    """On the relative fallback (PATH below the cwd), the kernel's errors
    come back as they are and nothing is tried after them: the place for the
    symlink (BASE, marked untouched by the test) stays untouched."""
    p = arg("PATH")
    base = arg("BASE")
    stale = arg("STALE")
    cfg = fallback_cfg([base])
    for q in (p, stale):
        # The premise: each path's relative spelling fits the plain sockaddr,
        # so the relative fallback is the one that fails.
        rel = _route.Relative.new(q, os.path.dirname(q))
        assert rel is not None
        assert len(rel.path) <= DIRECT_MAX, rel
    errors: dict[str, str] = {}
    for req in (CONNECT, CONNECT_NONBLOCKING):
        try:
            _route.run_long(req, p, cfg)
        except OSError as e:
            errors[f"no-socket {req.nonblocking}"] = str(e.errno)
        try:
            _route.run_long(req, stale, cfg)
        except OSError as e:
            errors[f"stale {req.nonblocking}"] = str(e.errno)
    assert_untouched(base)
    report(**errors)


def relative_input() -> None:
    """A long path given relative to the cwd (REL) is bound by the central
    route (D in the socket's directory) and connected to by the fallbacks
    (BASE as the only place for the symlink, no /proc), in the directory it
    names below the cwd; the routes are reported."""
    rel = arg("REL")
    listener, bind_route = _route.run_long(BIND, rel, central_cfg())
    with listener:
        assert is_socket(rel)
        # Reported as given, relative, as the standard reports a short
        # relative path it bound or connected to.
        assert listener.getsockname() == os.fsdecode(rel)
        client, connect_route = _route.run_long(CONNECT, rel, fallback_cfg([arg("BASE")]))
        with client:
            assert client.getpeername() == os.fsdecode(rel)
            accept_ping(listener, client)
    report(bind=bind_route.value, connect=connect_route.value)


UNRELATED = b"unrelated, not to be removed"
"""What the files that stand where a socket's spelling would name hold
(:func:`spelling_namesakes`)."""


def put_unrelated(path: bytes) -> None:
    with open(path, "wb") as f:
        f.write(UNRELATED)


def assert_unrelated(path: bytes, what: str) -> None:
    assert os.path.lexists(path), what
    with open(path, "rb") as f:
        assert f.read() == UNRELATED, what


def spelling_namesakes() -> None:
    """For each means of MEANS (comma-separated): bind PATH (far from the
    cwd, file name ``s.sock``) through it while a file ``s.sock`` unrelated to
    the socket sits in the cwd. The spelling the kernel keeps (``R/s.sock``,
    or behind ``/proc/self/fd/<n>/``) names nothing: ``os.stat`` and
    ``os.unlink`` of it are ``FileNotFoundError``, and the unrelated file is
    left alone. Then the same listener path is served by asyncio as the
    standard's is (:func:`_support.serve_asyncio`, bound before the loop):
    from Python 3.13 asyncio removes the file at the listener's address when
    the server closes, which must be the socket file at PATH, not the file in
    the cwd. Reports which means ran."""
    p = arg("PATH")
    name = os.path.basename(p)
    put_unrelated(name)
    checked: list[str] = []
    for means in os.fsdecode(arg("MEANS")).split(","):
        cfg, want = means_cfg(means, (SOCKET_DIR,))
        listener, r = _route.run_long(BIND, p, cfg)
        with listener:
            assert r is want, r
            kernel = os.fsencode(socket.socket.getsockname(listener))
            assert re.fullmatch(rb"(/proc/self/fd/\d+/)?[A-Za-z0-9]{10}/" + re.escape(name), kernel), kernel
            for f in (os.stat, os.unlink):
                try:
                    f(kernel)
                except FileNotFoundError:
                    pass
                else:
                    raise AssertionError(f"{means}: {f.__name__} of the kernel's spelling found something")
            assert_unrelated(name, f"{means}: the unrelated file in the cwd was touched")
        os.unlink(p)

        def connect(req: Request, cfg: Config = cfg) -> socket.socket:
            return _route.run_long(req, p, cfg)[0]

        listener, _ = _route.run_long(BIND, p, cfg)
        try:
            asyncio.run(serve_asyncio(means, listener, connect, os.fsdecode(p), served=True))
        finally:
            assert_unrelated(name, f"{means}: asyncio removed the unrelated file in the cwd")
        assert_cleaned_up_as_asyncio_does(means, p)
        checked.append(means)
    report(checked=",".join(checked))


def asyncio_namesakes() -> None:
    """The public API's listener at PATH (far from the cwd, file name
    ``s.sock``, bound by the central route), served by asyncio as the
    standard's is (:func:`_support.serve_asyncio`, bound before the loop,
    the public connects adopted), while a file ``s.sock`` unrelated to the
    socket sits in the cwd: asyncio's cleanup (Python 3.13 and later) removes
    the socket file at PATH, not that file."""
    p = arg("PATH")
    name = os.path.basename(p)
    put_unrelated(name)

    def connect(req: Request) -> socket.socket:
        return sockat.connect_nonblocking(p) if req.nonblocking else sockat.connect(p)

    try:
        asyncio.run(serve_asyncio("public", sockat.bind(p), connect, os.fsdecode(p), served=True))
    finally:
        assert_unrelated(name, "the unrelated file in the cwd was touched")
    assert_cleaned_up_as_asyncio_does("public", p)
    report()


def own_dir_where() -> None:
    """Bind PATH with the configuration read from the environment
    (``Config.from_env``) and report the route and the places D was made in
    (``st_dev:st_ino`` of each, comma-separated)."""
    made: list[str] = []
    real = _route.mkdir_at

    def mkdir_at(base_fd: int, name: bytes) -> None:
        real(base_fd, name)
        st = os.fstat(base_fd)
        made.append(f"{st.st_dev}:{st.st_ino}")

    _route.mkdir_at = mkdir_at
    listener, route = _route.run_long(BIND, arg("PATH"), Config.from_env())
    listener.close()
    report(route=route.value, places=",".join(made))


def bind_with_umask() -> None:
    """With the umask UMASK (octal), bind PATH through the means MEANS with
    PLACE as the only place for D, and report the route, or the errno it
    fails with. (A connect is not
    made: a umask without the owner's write bit makes the socket file itself
    one nobody may connect to, as with the standard.)"""
    os.umask(int(arg("UMASK"), 8))
    cfg, want = means_cfg(os.fsdecode(arg("MEANS")), (arg("PLACE"),))
    try:
        listener, route = _route.run_long(BIND, arg("PATH"), cfg)
    except OSError as e:
        report(errno=str(e.errno))
        return
    listener.close()
    assert route is want, route
    assert is_socket(arg("PATH"))
    report(route=route.value)


def cwd_changed() -> None:
    """The relative spelling is resolved against the cwd at the moment of the
    connect, and the cwd is shared by every thread, so another thread that
    changes it in between sends the call to another directory. When the cwd
    is still changed afterwards, ``Relative.run`` must notice and turn the
    result into an error even though the call itself succeeded.

    The change is made from inside the call (the function chdirs before it
    connects), standing in for another thread. PATH is below the cwd (ONE);
    OTHER has a listener at the same relative spelling, so the connect
    succeeds there."""
    p = arg("PATH")
    two = arg("OTHER")
    directory, _ = os.path.split(p)
    rel = _route.Relative.new(p, directory)
    assert rel is not None
    assert b".." not in rel.path.split(b"/"), rel.path
    # A listener in the other directory, at the same relative spelling (bound
    # from there: the spelling is short, the absolute path is not).
    one = os.getcwdb()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    os.chdir(two)
    listener.bind(rel.path)
    listener.listen()
    os.chdir(one)

    def chdir_then(q: bytes) -> socket.socket:
        os.chdir(two)
        return _route.plain_op(CONNECT, q)

    try:
        rel.run(chdir_then)
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
    report(connect=repr(connect_error))


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


def deleted_cwd() -> None:
    """With the cwd removed and $PWD unset, bind and both connects of
    ``./`` + a long file name are ENOENT, and nothing is made in the place
    for D (PLACE). On macOS getcwd still spells a removed cwd, so a realpath
    that did not check each component would take that spelling for a real
    directory."""
    place = arg("PLACE")
    here = os.getcwd()
    os.rmdir(here)
    os.environ.pop("PWD", None)
    cfg = central_cfg((place,))
    results: dict[str, str] = {}
    for n in (103, DIRECT_MAX):
        path = b"./" + b"n" * n
        for req, what in ((BIND, "bind"), (CONNECT, "connect"), (CONNECT_NONBLOCKING, "connect_nonblocking")):
            try:
                if len(path) > DIRECT_MAX:
                    _route.run_long(req, path, cfg)
                else:
                    _route.run(req, path)
            except OSError as e:
                results[f"{n}-{what}"] = str(e.errno)
            else:
                raise AssertionError(f"{what} of {path!r} in a removed cwd went through")
    assert os.listdir(place) == [], os.listdir(place)
    report(**results)


SCENARIOS: dict[str, Callable[[], None]] = {
    f.__name__: f
    for f in (
        make_stale_socket,
        connect_report,
        relative_spelling,
        bind_without_own_dir,
        relative_routes,
        relative_failures,
        relative_input,
        spelling_namesakes,
        asyncio_namesakes,
        own_dir_where,
        bind_with_umask,
        cwd_changed,
        default_backlog,
        deleted_cwd,
    )
}


if __name__ == "__main__":
    SCENARIOS[sys.argv[1]]()
