"""``sockat.ex``: what the calls return (the route, B, the routes skipped,
``Spelling``), ``remove()`` and ``cleanup()`` (hub SPEC "片付け"), the
errors with ``skipped`` and ``route``, and the resolve calls. B is given
(``Options.base``) so that the tests keep to their own directories."""

from __future__ import annotations

import contextlib
import errno
import os
import socket
from collections.abc import Callable

import pytest
from _support import (
    HAS_THREAD_ROUTE,
    NO_PROC,
    NO_ROUTE_CODE,
    ROOT,
    TmpDirs,
    accept_ping,
    deep_dir,
    ds,
    env_at,
    is_socket,
    kernel_sockname,
    listener_at,
    long_path,
    nest,
    raises_errno,
    run_child,
    standard_errno,
)

import sockat.ex
from sockat._place import LinkFile, default_link_file
from sockat._spelling import Trail
from sockat._sys import DIRECT_MAX, IS_FREEBSD


def options(tmp: TmpDirs) -> sockat.ex.Options:
    return sockat.ex.Options(base=tmp.new() + b"/b")


def e_in(o: sockat.ex.Options) -> list[str]:
    """The E in the D of B ``o.base`` (all D of it)."""
    assert o.base is not None
    b = os.fsencode(o.base)
    return sorted(os.fsdecode(e) for d in ds(b) for e in os.listdir(b + b"/" + d))


# bind's Bound: the listening socket, the file, the spelling, the route, B and
# the routes skipped; it unpacks as a tuple in that order.
def test_bound(tmp: TmpDirs) -> None:
    o = options(tmp)
    p = long_path(tmp.new())
    bound = sockat.ex.bind(p, options=o)
    sock, file, spelling, route, base, skipped = bound
    with sock:
        assert route == "link-dir"
        assert base == "given"
        assert isinstance(spelling, sockat.ex.Linked)
        assert [s.name for s in skipped] == ["standard"]
        assert all(isinstance(s, sockat.ex.Skipped) for s in skipped)
        assert sock.getsockname() == os.fsdecode(p)
        c = sockat.ex.connect(p)
        with c.socket:
            accept_ping(sock, c.socket)
        file.remove()


# remove() removes the socket file and the E of the spelling, and leaves the
# listener open (connections already queued are still accepted). Later calls
# do nothing.
def test_remove_removes_the_file_and_e_not_the_listener(tmp: TmpDirs) -> None:
    o = options(tmp)
    p = long_path(tmp.new())
    bound = sockat.ex.bind(p, options=o)
    with bound.socket as listener:
        c = sockat.ex.connect(p)
        assert len(e_in(o)) == 1
        bound.file.remove()
        assert not os.path.lexists(p)
        assert e_in(o) == []
        with c.socket:
            accept_ping(listener, c.socket)
        bound.file.remove()
        bound.file.cleanup()


# remove() leaves alone a file put at the path in place of the socket file
# while the listener was open.
def test_remove_leaves_a_replacement_alone(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    bound = sockat.ex.bind(p, options=options(tmp))
    with bound.socket:
        os.unlink(p)
        with open(p, "wb") as f:
            f.write(b"replacement")
        bound.file.remove()
        with open(p, "rb") as f:
            assert f.read() == b"replacement"


# remove() succeeds when the file is gone already.
def test_remove_when_gone(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    bound = sockat.ex.bind(p, options=options(tmp))
    with bound.socket:
        os.unlink(p)
        bound.file.remove()


# cleanup() removes E only: the socket file stays, the listener serves, and
# the spelling names nothing afterwards.
def test_cleanup_removes_e_only(tmp: TmpDirs) -> None:
    o = options(tmp)
    p = long_path(tmp.new())
    bound = sockat.ex.bind(p, options=o)
    with bound.socket as listener:
        spelling = os.fsencode(kernel_sockname(listener))
        bound.file.cleanup()
        assert e_in(o) == []
        assert is_socket(p)
        assert not os.path.lexists(spelling)
        c = sockat.ex.connect(p)
        with c.socket:
            accept_ping(listener, c.socket)
        bound.file.remove()
    assert not os.path.lexists(p)


# For a short path remove() removes the file, and the listener does not
# remove it when closed (bind_ex's never do).
def test_remove_of_a_short_path(tmp: TmpDirs) -> None:
    p = tmp.new() + b"/s.sock"
    bound = sockat.ex.bind(p)
    assert bound.spelling == sockat.ex.Original()
    bound.socket.close()
    assert is_socket(p)
    bound.file.remove()
    assert not os.path.lexists(p)


# A relative path bound as it is (no absolute candidate and no route usable:
# SPEC "相対パスを絶対パスにする" 5) whose cwd changes between the start of the
# call and the moment the file is remembered: the listener is closed and the
# error raised (no errno), the file left; remove() could otherwise remove an
# unrelated file of the same name under another cwd.
def test_a_relative_bind_with_the_cwd_changed(tmp: TmpDirs) -> None:
    a, other = tmp.new(), tmp.new()
    out = run_child(
        NO_ROUTE_CODE
        + """
import sockat.ex
from sockat import _socket_file
real = os.path.realpath
def moved(p, **kw):
    os.chdir(ARGS["other"])
    return real(p, **kw)
_socket_file.os.path.realpath = moved
try:
    sockat.ex.bind("s.sock")
    report(error="none")
except OSError as e:
    report(error=type(e).__name__, errno=e.errno, route=e.route)
""",
        cwd=a,
        args={"other": os.fsdecode(other)},
    )
    assert out["error"] == "CwdChanged"
    assert out["errno"] is None
    assert out["route"] == "standard"
    assert is_socket(a + b"/s.sock")


# A failed call raises the plain call's error (its type and errno kept) with
# `skipped` (up to the failure) and `route` (the route whose bind / connect
# failed, None when none got that far).
def test_errors_carry_skipped_and_route(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    with open(p, "wb"):
        pass
    with raises_errno(standard_errno("bind-exists")) as e:
        sockat.ex.bind(p, options=options(tmp))
    assert e.value.route == "link-dir"  # type: ignore[attr-defined]
    assert [s.name for s in e.value.skipped] == ["standard"]  # type: ignore[attr-defined]
    os.unlink(p)
    with raises_errno(standard_errno("connect-missing")) as fe:
        sockat.ex.connect(p)
    assert fe.value.route is not None  # type: ignore[attr-defined]
    assert fe.value.skipped[0].name == "standard"  # type: ignore[attr-defined]


# connect / connect_nonblocking return the socket, the route and the routes
# skipped; nonblocking leaves the socket nonblocking.
def test_connected(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    with listener_at(p) as listener:
        for call, blocking in ((sockat.ex.connect, True), (sockat.ex.connect_nonblocking, False)):
            c = call(p)
            sock, route, skipped = c
            with sock:
                assert sock.getblocking() is blocking
                assert route != "standard"
                assert skipped[0].name == "standard"
                accept_ping(listener, sock)


# resolve_for_bind gives a spelling the standard binds (any type), naming the
# path; cleanup() removes its E. A short path comes back as it is, of the
# type given.
def test_resolve_for_bind(tmp: TmpDirs) -> None:
    o = options(tmp)
    p = long_path(tmp.new())
    r = sockat.ex.resolve_for_bind(os.fsdecode(p), options=o)
    assert isinstance(r.path, str)
    assert len(os.fsencode(r.path)) <= DIRECT_MAX
    assert r.route == "link-dir"
    assert isinstance(r.spelling, sockat.ex.Linked)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.bind(r.path)
        assert is_socket(p)
    r.cleanup()
    assert e_in(o) == []
    short = tmp.new() + b"/s.sock"
    rs = sockat.ex.resolve_for_bind(short)
    assert rs.path == short
    assert rs.spelling == sockat.ex.Original()
    assert rs.route == "standard"


# resolve_for_bind does not go to thread-cwd / at: a name only they reach is
# ENAMETOOLONG (link-file aside where a bind follows it).
def test_resolve_for_bind_past_link_dir(tmp: TmpDirs) -> None:
    p = long_path(tmp.new(), name=b"n" * 95)
    if LinkFile.FOLLOWS is default_link_file():
        r = sockat.ex.resolve_for_bind(p, options=options(tmp))
        assert r.route == "link-file"
        r.cleanup()
        return
    with raises_errno(errno.ENAMETOOLONG) as e:
        sockat.ex.resolve_for_bind(p, options=options(tmp))
    assert [s.name for s in e.value.skipped] == ["standard", "link-dir"]  # type: ignore[attr-defined]


# resolve_for_connect gives a spelling to connect to as often as needed
# (link-file: a symlink to the socket file, whatever the file name), then
# cleanup().
def test_resolve_for_connect(tmp: TmpDirs) -> None:
    o = options(tmp)
    p = long_path(tmp.new(), name=b"n" * 95)
    with listener_at(p) as listener:
        r = sockat.ex.resolve_for_connect(p, options=o)
        assert r.route == "link-file"
        assert isinstance(r.path, bytes)
        for _ in range(3):
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(r.path)
                accept_ping(listener, s)
        r.cleanup()
        assert e_in(o) == []
        with socket.socket(socket.AF_UNIX) as s, raises_errno(standard_errno("connect-missing")):
            s.connect(r.path)
        assert not hasattr(r, "remove")
    short = nest(tmp.new(), 40)
    assert sockat.ex.resolve_for_connect(short).path == short


# sockat.ex.sweep() sweeps the places read from the environment: run in a
# child whose places are the test's own (the real /tmp/sockat-<uid> holds
# other processes' D, which a test must not remove), it removes the D of a
# process gone and keeps the live one's.
def test_sweep_sweeps_the_places_of_the_environment(tmp: TmpDirs) -> None:
    place = tmp.new()
    work = tmp.new()
    ended = run_child(
        """
        from _support import env_at, bind_in, long_path
        bind_in(env_at(os.fsencode(ARGS["place"])), long_path(os.fsencode(ARGS["work"]) + b"/ended"))
        """,
        args={"place": os.fsdecode(place), "work": os.fsdecode(work)},
    )
    assert ended["returncode"] == 0
    out = run_child(
        """
        import sockat.ex
        from sockat import _place
        from _support import env_at, bind_in, long_path, ds
        env = env_at(os.fsencode(ARGS["place"]))
        _place.Config.from_env = classmethod(lambda cls: env.config)
        bind_in(env, long_path(os.fsencode(ARGS["work"]) + b"/live"))
        b = env.auto_base().real
        before = len(ds(b))
        sockat.ex.sweep()
        report(before=before, after=ds(b) == [env.auto_base().dir(env.config.uid).name])
        """,
        args={"place": os.fsdecode(place), "work": os.fsdecode(work)},
    )
    assert out["before"] == 2
    assert out["after"] is True


# ForBind has no remove(): after binding the spelling oneself, socket_file()
# follows it and remembers the socket file; its remove() removes that file
# (and E), and leaves a replacement alone, as Bound.file's does. Before the
# bind there is nothing to follow.
def test_for_bind_socket_file(tmp: TmpDirs) -> None:
    o = options(tmp)
    p = long_path(tmp.new())
    r = sockat.ex.resolve_for_bind(p, options=o)
    assert not hasattr(r, "remove")
    with pytest.raises(FileNotFoundError):
        r.socket_file()
    with socket.socket(socket.AF_UNIX) as s:
        s.bind(r.path)
        s.listen()
        f = r.socket_file()
        assert is_socket(p)
        f.remove()
        assert not os.path.lexists(p)
        assert e_in(o) == []
    # A file put in its place while the listener is open is not removed.
    r = sockat.ex.resolve_for_bind(p, options=o)
    with socket.socket(socket.AF_UNIX) as s:
        s.bind(r.path)
        f = r.socket_file()
        os.unlink(p)
        with open(p, "wb") as fh:
            fh.write(b"replacement")
        f.remove()
        with open(p, "rb") as fh:
            assert fh.read() == b"replacement"


# A relative spelling (a short relative path given back as it is: no
# absolute candidate and no route usable) whose cwd changed between the
# resolve and socket_file() is refused (no errno): it would remember a file
# under another cwd.
def test_for_bind_socket_file_relative_cwd_changed(tmp: TmpDirs) -> None:
    a, other = tmp.new(), tmp.new()
    out = run_child(
        NO_ROUTE_CODE
        + """
import sockat.ex
r = sockat.ex.resolve_for_bind("s.sock")
assert r.spelling == sockat.ex.CwdRelative() and r.path == "s.sock"
s = socket.socket(socket.AF_UNIX)
s.bind(r.path)
os.chdir(ARGS["other"])
try:
    r.socket_file()
    report(error="none")
except OSError as e:
    report(error=type(e).__name__, errno=e.errno)
""",
        cwd=a,
        args={"other": os.fsdecode(other)},
    )
    assert out == {**out, "error": "CwdChanged", "errno": None}


# ForConnect carries no diagnostic base (a connect's B is not the caller's
# concern), only route and skipped.
def test_for_connect_has_no_base(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    with listener_at(p):
        r = sockat.ex.resolve_for_connect(p, options=options(tmp))
        assert not hasattr(r, "base")
        r.cleanup()


# remove() that fails (the socket's directory cannot be searched: lstat
# answers EACCES) raises, still does cleanup(), and a later call, once the
# directory can be searched again, removes the file.
@pytest.mark.skipif(ROOT, reason="root searches anything")
def test_remove_that_fails(tmp: TmpDirs) -> None:
    o = options(tmp)
    p = long_path(tmp.new())
    bound = sockat.ex.bind(p, options=o)
    d = os.path.dirname(p)
    with bound.socket:
        os.chmod(d, 0o600)
        try:
            with pytest.raises(PermissionError):
                bound.file.remove()
            assert e_in(o) == []
        finally:
            os.chmod(d, 0o700)
        assert is_socket(p)
        bound.file.remove()
        assert not os.path.lexists(p)


# On a system whose bind follows link-file's symlink, socket_file() follows
# a link-file spelling to the socket file at the path.
@pytest.mark.skipif(default_link_file() is not LinkFile.FOLLOWS, reason="resolve_for_bind has link-file here only")
def test_for_bind_socket_file_through_link_file(tmp: TmpDirs) -> None:
    o = options(tmp)
    p = long_path(tmp.new(), name=b"n" * 100)
    r = sockat.ex.resolve_for_bind(p, options=o)
    assert r.route == "link-file"
    with socket.socket(socket.AF_UNIX) as s:
        s.bind(r.path)
        f = r.socket_file()
        assert is_socket(p)
        f.remove()
    assert not os.path.lexists(p)
    assert e_in(o) == []


# A long path given relative to the cwd is bound, reached and removed like an
# absolute one: the socket at the path, remove() removing it.
def test_a_long_relative_path(tmp: TmpDirs) -> None:
    root = tmp.new()
    p = long_path(root)
    rel = os.path.relpath(p, root)
    assert len(rel) > DIRECT_MAX
    out = run_child(
        """
        import sockat, sockat.ex
        o = sockat.ex.Options(base=os.fsencode(ARGS["given"]))
        bound = sockat.ex.bind(ARGS["rel"], options=o)
        c = sockat.ex.connect(ARGS["rel"], options=o)
        conn, _ = bound.socket.accept()
        c.socket.sendall(b"x")
        got = conn.recv(1)
        here = os.path.exists(ARGS["rel"])
        bound.file.remove()
        report(route=bound.route, got=got.decode(), here=here, gone=not os.path.lexists(ARGS["rel"]))
        """,
        cwd=root,
        args={"rel": os.fsdecode(rel), "given": os.fsdecode(tmp.new() + b"/b")},
    )
    assert out["route"] == "link-dir"
    assert out["got"] == "x"
    assert out["here"] is True
    assert out["gone"] is True


# socket_file() before the bind, with an unrelated regular file at the path:
# not a socket, so an error (ENOTSOCK), and the file stays (remove() could
# otherwise remove it).
def test_for_bind_socket_file_refuses_a_file_that_is_not_a_socket(tmp: TmpDirs) -> None:
    o = options(tmp)
    p = long_path(tmp.new())
    with open(p, "wb") as f:
        f.write(b"unrelated")
    r = sockat.ex.resolve_for_bind(p, options=o)
    with raises_errno(errno.ENOTSOCK):
        r.socket_file()
    with open(p, "rb") as f:
        assert f.read() == b"unrelated"
    r.cleanup()


# Where a bind follows a symlink in the last component (macOS, NetBSD), the
# socket is made where it points; bind_ex remembers that file, so remove()
# removes the socket and leaves the caller's symlink. For a short path (the
# standard's own bind) and a long one.
@pytest.mark.skipif(default_link_file() is not LinkFile.FOLLOWS, reason="a bind follows the symlink here only")
@pytest.mark.parametrize("long", [False, True])
def test_remove_follows_a_symlink_in_the_last_component(tmp: TmpDirs, long: bool) -> None:
    there = tmp.new() + b"/real.sock"
    d = os.path.dirname(long_path(tmp.new())) if long else tmp.new()
    link = d + b"/s.sock"
    os.symlink(there, link)
    bound = sockat.ex.bind(link, options=options(tmp))
    with bound.socket:
        assert is_socket(there)
        bound.file.remove()
    assert not os.path.lexists(there)
    assert os.path.islink(link)


# What the bind leaves at the path must be a socket to be remembered: a
# symlink in the last component to something else (made between the bind and
# the remembering) is refused, so remove() could never remove it.
def test_a_symlink_to_no_socket_is_not_remembered(tmp: TmpDirs) -> None:
    from sockat._socket_file import At, Recorder

    target = tmp.new() + b"/file"
    with open(target, "wb"):
        pass
    link = tmp.new() + b"/s.sock"
    os.symlink(target, link)
    with raises_errno(errno.ENOTSOCK):
        Recorder(link).bound(At(link))


# Cleaning up E that fails (here: unlink refused) changes neither the call's
# outcome nor its error: a connect through link-file goes through, a bind
# whose bind fails raises its own error.
def test_failing_to_clean_up_e_is_silent(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    from sockat import _calls
    from sockat._routes import Call
    from sockat._sys import Op

    real_unlink = os.unlink

    def unlink(path: bytes, *, dir_fd: int | None = None) -> None:
        if dir_fd is not None:
            raise PermissionError(13, "refused for the test")
        real_unlink(path)

    env = env_at(tmp.new(), proc=NO_PROC)
    p = long_path(tmp.new())
    with listener_at(p) as listener:
        monkeypatch.setattr(os, "unlink", unlink)
        sock, route = _calls.connect(p, call=Call(Op.CONNECT), env=env, order=("link-file",))
        monkeypatch.undo()
        with sock:
            accept_ping(listener, sock)
        assert route == "link-file"
    monkeypatch.setattr(os, "unlink", unlink)
    with raises_errno(standard_errno("bind-exists")):
        _calls.bind(p, env=env)
    monkeypatch.undo()


def _d_of(o: sockat.ex.Options) -> bytes:
    assert o.base is not None
    b = os.fsencode(o.base)
    (d,) = ds(b)
    return b + b"/" + d


# cleanup() asked for by the caller reports a failure to remove E (here: D
# not writable): raised, E left, and a later call (once it can) removes it;
# after that, calls do nothing. For SocketFile, ForBind and ForConnect.
@pytest.mark.skipif(ROOT, reason="root writes anywhere")
@pytest.mark.parametrize("which", ["socket file", "for bind", "for connect"])
def test_cleanup_reports_its_failure(tmp: TmpDirs, which: str) -> None:
    o = options(tmp)
    p = long_path(tmp.new())
    with listener_at(p) if which == "for connect" else contextlib.nullcontext():
        if which == "socket file":
            bound = sockat.ex.bind(p, options=o)
            cleanup = bound.file.cleanup
        elif which == "for bind":
            cleanup = sockat.ex.resolve_for_bind(p, options=o).cleanup
        else:
            cleanup = sockat.ex.resolve_for_connect(p, options=o).cleanup
        d = _d_of(o)
        os.chmod(d, 0o511)
        try:
            with pytest.raises(PermissionError):
                cleanup()
            assert e_in(o) != []
        finally:
            os.chmod(d, 0o711)
        cleanup()
        assert e_in(o) == []
        cleanup()


# remove(): when both the file's removal and cleanup() fail, the file's error
# is raised (cleanup()'s noted on it); when only cleanup() fails, its error is
# raised and the file is gone. cleanup() is done in either case.
@pytest.mark.skipif(ROOT, reason="root writes anywhere")
def test_remove_raises_the_files_error_first(tmp: TmpDirs) -> None:
    o = options(tmp)
    p = long_path(tmp.new())
    bound = sockat.ex.bind(p, options=o)
    d, sockdir = _d_of(o), os.path.dirname(p)
    with bound.socket:
        os.chmod(d, 0o511)
        os.chmod(sockdir, 0o600)
        try:
            with pytest.raises(PermissionError) as e:
                bound.file.remove()
            assert any("cleaning up" in n for n in getattr(e.value, "__notes__", []))
        finally:
            os.chmod(sockdir, 0o700)
        try:
            with pytest.raises(PermissionError) as e2:
                bound.file.remove()
            assert not getattr(e2.value, "__notes__", [])
            assert not os.path.lexists(p)
        finally:
            os.chmod(d, 0o711)
        bound.file.remove()
        assert e_in(o) == []


# remove() / cleanup() called from many threads at once remove one thing at a
# time (SPEC "remove と cleanup"): among one Bound.file, and among a ForBind
# and the SocketFile its socket_file() gave (they share E). Each unlink is
# held open a moment to let them overlap if they could; the most at once must
# be 1, and the file and E end up removed once.
@pytest.mark.parametrize("which", ["bound", "for bind"])
def test_concurrent_removals_are_one_at_a_time(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, which: str) -> None:
    import threading
    import time

    o = options(tmp)
    p = long_path(tmp.new())
    keep: list[socket.socket] = []
    calls: list[Callable[[], None]] = []
    if which == "bound":
        bound = sockat.ex.bind(p, options=o)
        keep.append(bound.socket)
        calls = [bound.file.remove, bound.file.cleanup] * 4
    else:
        r = sockat.ex.resolve_for_bind(p, options=o)
        s = socket.socket(socket.AF_UNIX)
        s.bind(r.path)
        keep.append(s)
        files = [r.socket_file() for _ in range(3)]
        calls = [r.cleanup, *(f.remove for f in files), *(f.cleanup for f in files)] * 2
    real_unlink = os.unlink
    lock = threading.Lock()
    now = [0]
    most = [0]

    def unlink(path: bytes, *, dir_fd: int | None = None) -> None:
        with lock:
            now[0] += 1
            most[0] = max(most[0], now[0])
        try:
            time.sleep(0.02)
            real_unlink(path, dir_fd=dir_fd)
        finally:
            with lock:
                now[0] -= 1

    monkeypatch.setattr(os, "unlink", unlink)
    start = threading.Barrier(len(calls))
    errors: list[BaseException] = []

    def run(call: Callable[[], None]) -> None:
        start.wait()
        try:
            call()
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=run, args=(c,)) for c in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    monkeypatch.undo()
    for k in keep:
        k.close()
    assert errors == []
    assert most[0] == 1
    assert e_in(o) == []
    if which == "bound":
        assert not os.path.lexists(p)


# What bind_ex remembers must be a socket even when the last component is no
# symlink: a regular file put at the path right after the bind (before it is
# remembered) is refused (ENOTSOCK): the listener is closed, E cleaned up,
# and the file left (remove() could otherwise remove it). For a short path
# (the standard) and the routes of a long one.
@pytest.mark.parametrize("route", ["standard", "link-dir", *(["thread-cwd"] if HAS_THREAD_ROUTE else [])])
def test_a_file_put_in_place_of_the_socket_is_not_remembered(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    from sockat import _calls
    from sockat._place import LinkFile as LF
    from sockat._socket_file import At, Recorder

    if route == "standard":
        p = tmp.new() + b"/s.sock"
        env = env_at(tmp.new())
    else:
        place = tmp.new() if route == "link-dir" else deep_dir(tmp.new(), 90)
        env = env_at(place, proc=NO_PROC, link_file=LF.NOT_TRIED)
        p = long_path(tmp.new())
    real_bound = Recorder.bound
    closed: list[bool] = []

    def swapped(self: Recorder, at: At) -> None:
        os.unlink(p)
        with open(p, "wb") as f:
            f.write(b"replacement")
        real_bound(self, at)

    monkeypatch.setattr(Recorder, "bound", swapped)
    real_close = socket.socket.close

    def close(s: socket.socket) -> None:
        closed.append(True)
        real_close(s)

    monkeypatch.setattr(socket.socket, "close", close)
    trail = Trail()
    with raises_errno(errno.ENOTSOCK):
        _calls.bind(p, env=env, remember=True, trail=trail)
    monkeypatch.undo()
    assert trail.trying == ("at" if route == "thread-cwd" and IS_FREEBSD else route)
    assert closed
    with open(p, "rb") as f:
        assert f.read() == b"replacement"
    if route != "standard":
        b = env.auto_base()
        assert b is not None
        assert all(os.listdir(b.real + b"/" + d) == [] for d in ds(b.real))


def _turn_on(monkeypatch: pytest.MonkeyPatch, when: str, turn: Callable[[], None]) -> None:
    """Call ``turn`` once, either just before the bind (the route already
    built: E made, pointing where the path led then) or just after it (before
    the socket file is remembered)."""
    from sockat import _routes, _sys
    from sockat._socket_file import At, Recorder

    done: list[bool] = []

    def once() -> None:
        if not done:
            done.append(True)
            turn()

    if when == "before the bind":
        real_bind = socket.socket.bind
        real_op_at = _sys.op_at

        def bind(s: socket.socket, address: str | bytes) -> None:
            once()
            real_bind(s, address)

        def op_at(which: _sys.Op, sock: socket.socket, dirfd: int, addr: bytes) -> None:
            once()
            real_op_at(which, sock, dirfd, addr)

        monkeypatch.setattr(socket.socket, "bind", bind)
        monkeypatch.setattr(_routes, "op_at", op_at)
    else:
        real_bound = Recorder.bound

        def bound(self: Recorder, at: At) -> None:
            once()
            real_bound(self, at)

        monkeypatch.setattr(Recorder, "bound", bound)


# link-dir, thread-cwd and at remember where they made the socket: the real
# directory E points to and the file name, not the path looked up again (SPEC
# "remove"). Here a symlink on the way (<holder>/sym -> a) is turned to b after
# the route was built, before or after the bind: the socket is made in a (E
# points there), and remove() removes a/s.sock and leaves alone another
# listener's socket at b/s.sock, which looking the path up again would have
# remembered (it is a socket) and removed. a's real path is long, so that the
# path goes to the routes rather than to the standard by its real candidate.
@pytest.mark.parametrize("when", ["before the bind", "after the bind"])
@pytest.mark.parametrize("route", ["link-dir", *(["thread-cwd"] if HAS_THREAD_ROUTE else [])])
def test_where_the_route_made_the_socket_is_remembered(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, route: str, when: str
) -> None:
    from sockat import _calls
    from sockat._place import LinkFile as LF

    a, b = os.path.realpath(deep_dir(tmp.new(), 110)), os.path.realpath(tmp.new())
    sym = deep_dir(tmp.new(), 120) + b"/sym"
    os.symlink(a, sym)
    p = sym + b"/s.sock"
    assert len(p) > DIRECT_MAX
    assert len(a + b"/s.sock") > DIRECT_MAX
    place = tmp.new() if route == "link-dir" else deep_dir(tmp.new(), 90)
    env = env_at(place, proc=NO_PROC, link_file=LF.NOT_TRIED)

    def turn() -> None:
        os.unlink(sym)
        os.symlink(b, sym)

    with listener_at(b + b"/s.sock"):
        _turn_on(monkeypatch, when, turn)
        trail = Trail()
        done, file = _calls.bind(p, env=env, remember=True, trail=trail)
        monkeypatch.undo()
        assert file is not None
        with done.sock:
            assert done.route == ("at" if route == "thread-cwd" and IS_FREEBSD else route)
            assert os.readlink(sym) == b
            assert is_socket(a + b"/s.sock")
            file.remove()
            assert not os.path.lexists(a + b"/s.sock")
            assert is_socket(b + b"/s.sock")


# The standard and link-file remember the realpath of the path looked up after
# the bind (SPEC "remove"): the bind follows the path as it is then. Here the
# symlink on the way (<holder>/sym -> a) is turned to b just before the bind:
# the socket is made in b, and remove() removes it and leaves alone another
# listener's socket at a/s.sock (where the path led when the call began).
@pytest.mark.parametrize("route", ["standard", *(["link-file"] if default_link_file() is LinkFile.FOLLOWS else [])])
def test_the_standard_and_link_file_remember_the_path_after_the_bind(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    from sockat import _calls

    a, b = os.path.realpath(tmp.new()), os.path.realpath(tmp.new())
    name = b"s.sock" if route == "standard" else b"n" * 100
    sym = tmp.new() + b"/sym"
    os.symlink(a, sym)
    p = sym + b"/" + name
    assert (len(p) <= DIRECT_MAX) is (route == "standard")
    env = env_at(tmp.new())

    def turn() -> None:
        os.unlink(sym)
        os.symlink(b, sym)

    with listener_at(a + b"/" + name):
        _turn_on(monkeypatch, "before the bind", turn)
        done, file = _calls.bind(p, env=env, remember=True)
        monkeypatch.undo()
        assert file is not None
        with done.sock:
            assert done.route == route
            assert is_socket(b + b"/" + name)
            file.remove()
            assert not os.path.lexists(b + b"/" + name)
            assert is_socket(a + b"/" + name)


def _sym_dotdot(root: bytes, long: bool) -> tuple[bytes, bytes, bytes]:
    """``<root>/a/.../sym -> <root>/b/.../target``, and the path
    ``<root>/a/.../sym/../s.sock``: the kernel takes its directory as
    ``<root>/b/...`` (the target's parent); folding ``sym/..`` as text would
    give ``<root>/a/...``. Returns the path, the socket file the kernel means
    and the one a textual fold would mean."""
    mid = b"/" + b"x" * 60 + b"/" + b"x" * 60 if long else b""
    a, b = root + b"/a" + mid, root + b"/b" + mid
    os.makedirs(a)
    os.makedirs(b + b"/target")
    os.symlink(b + b"/target", a + b"/sym")
    return a + b"/sym/../s.sock", b + b"/s.sock", a + b"/s.sock"


# A path through a symlink and then ".." is not folded as text: the bind
# (link-dir's E pointing at the directory the kernel means), a connect, and
# remove() all work on <root>/b/..., as the kernel does; an unrelated file at
# the folded place <root>/a/s.sock is left alone.
def test_a_symlink_then_dotdot_is_not_folded(tmp: TmpDirs) -> None:
    o = options(tmp)
    p, meant, folded = _sym_dotdot(os.path.realpath(tmp.new()), long=True)
    with open(folded, "wb") as f:
        f.write(b"unrelated")
    bound = sockat.ex.bind(p, options=o)
    with bound.socket as listener:
        assert bound.route == "link-dir"
        assert is_socket(meant)
        spelling = os.fsencode(kernel_sockname(listener))
        assert os.path.realpath(os.path.dirname(spelling)) == os.path.dirname(meant)
        c = sockat.ex.connect(p)
        with c.socket:
            accept_ping(listener, c.socket)
        bound.file.remove()
    assert not os.path.lexists(meant)
    with open(folded, "rb") as f:
        assert f.read() == b"unrelated"


# The same with a short relative path (the standard's bind) given from <root>:
# remove() removes the socket the kernel made, not a file at the folded place.
def test_a_short_relative_symlink_then_dotdot_is_not_folded(tmp: TmpDirs) -> None:
    root = os.path.realpath(tmp.new())
    p, meant, folded = _sym_dotdot(root, long=False)
    with open(folded, "wb") as f:
        f.write(b"unrelated")
    out = run_child(
        """
        import sockat.ex
        b = sockat.ex.bind(ARGS["rel"])
        c = sockat.ex.connect(ARGS["rel"])
        b.file.remove()
        report(route=b.route)
        """,
        cwd=root,
        # Spelled out (os.path.relpath would fold "sym/.." as text).
        args={"rel": os.fsdecode(p[len(root) + 1 :])},
    )
    assert out["route"] == "standard"
    assert not os.path.lexists(meant)
    with open(folded, "rb") as f:
        assert f.read() == b"unrelated"
