"""``sockat.ex``: what the calls return (the route, B, the routes skipped,
``Spelling``), ``remove()`` and ``cleanup()`` (hub SPEC "片付け"), the
errors with ``skipped`` and ``route``, and the resolve calls. B is given
(``Options.base``) so that the tests keep to their own directories."""

from __future__ import annotations

import errno
import os
import socket

import pytest
from _support import (
    ROOT,
    TmpDirs,
    accept_ping,
    ds,
    is_socket,
    kernel_sockname,
    listener_at,
    long_path,
    nest,
    raises_errno,
    run_child,
)

import sockat.ex
from sockat._place import LinkFile, default_link_file
from sockat._sys import DIRECT_MAX


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


# A relative path whose cwd changes between the start of the call and the
# moment the file is remembered: the listener is closed and the error raised
# (no errno), the file left; remove() could otherwise remove an unrelated
# file of the same name under another cwd.
def test_a_relative_bind_with_the_cwd_changed(tmp: TmpDirs) -> None:
    a, other = tmp.new(), tmp.new()
    out = run_child(
        """
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
    with raises_errno(errno.EADDRINUSE) as e:
        sockat.ex.bind(p, options=options(tmp))
    assert e.value.route == "link-dir"  # type: ignore[attr-defined]
    assert [s.name for s in e.value.skipped] == ["standard"]  # type: ignore[attr-defined]
    os.unlink(p)
    with pytest.raises(FileNotFoundError) as fe:
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
    assert sockat.ex.resolve_for_bind("s.sock").spelling == sockat.ex.CwdRelative()


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
        with socket.socket(socket.AF_UNIX) as s, pytest.raises(FileNotFoundError):
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


# A relative spelling (a short relative path) whose cwd changed between the
# resolve and socket_file() is refused (no errno): it would remember a file
# under another cwd.
def test_for_bind_socket_file_relative_cwd_changed(tmp: TmpDirs) -> None:
    a, other = tmp.new(), tmp.new()
    out = run_child(
        """
        import sockat.ex
        r = sockat.ex.resolve_for_bind("s.sock")
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
