"""The proc-held route of a connect (Linux with /proc): D made as the
routes through D make it, in the first of the places for D where it can be
(whether the place is safe or not does not matter: D is held by its fd), with
``s`` in it, a symlink to the socket file, and the connect to
``/proc/self/fd/<fd of D>/s``.

The socket paths here have a file name of 100 bytes, which fits neither
behind ``/proc/self/fd/<n>/`` (so proc-direct cannot be used) nor in
``R/<name>`` (so the routes through D cannot be used), 600 bytes deep under /tmp
(so no relative spelling fits). No bind reaches such a name, so the
listeners are bound by the standard under a short name and renamed
(:func:`_support.listener_at`). What D is made of (its name, mode, the check
of a replaced D, the names drawn) is shared with the routes through D and
checked in ``test_through_d.py``."""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import os
import re
import socket
import stat
import sys
from collections.abc import Sequence

import pytest
from _support import (
    NO_PLACE,
    PROC,
    UNTOUCHED,
    TmpDirs,
    accept_ping,
    assert_empty,
    assert_far_from_cwd,
    assert_untouched,
    at_once,
    check_unreachable,
    dir_id,
    is_cloexec,
    kernel_peername,
    kernel_sockname,
    listener_at,
    listing,
    mark_untouched,
    nest,
    parent,
    peer_pid,
    ping,
    raises_errno,
    run_child,
    serve_asyncio,
    stale_socket,
    with_name,
)

from sockat import _route
from sockat._route import BIND, CONNECT, CONNECT_NONBLOCKING, OWN_DIR_PREFIX, RANDOM_LEN, Config, Request, Route

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the proc-held route needs Linux's /proc")

NAME = b"n" * 100
"""A file name that fits neither behind ``/proc/self/fd/<n>/`` nor in
``R/<name>``."""


def fd_cfg(own: Sequence[bytes], links: Sequence[bytes] = (), *, proc: bytes = PROC) -> Config:
    """``own`` as the places for D, ``links`` as the places for the
    symlink, the real /proc unless ``proc`` says where else to look, the
    thread's own cwd off."""
    return Config(own_dir_places=tuple(own), link_places=tuple(links), proc_self_fd=proc, thread_cwd=False)


def far_path(tmp: TmpDirs, name: bytes = NAME) -> bytes:
    """A socket path 600 bytes deep under /tmp with the file name ``name``,
    far from the cwd (no relative spelling fits)."""
    p = nest(tmp.new(), 600, name)
    assert_far_from_cwd(p)
    return p


def assert_used(d: bytes) -> None:
    """Something was created in ``d`` and removed again since
    :func:`_support.mark_untouched` (its mtime moved), and nothing is left."""
    assert os.stat(d).st_mtime != UNTOUCHED, f"nothing was made in {d!r}"
    assert_empty(d)


def check_proc_held(p: bytes, cfg: Config, held_in: bytes) -> None:
    """Connect (blocking and nonblocking) to a listener at ``p`` through
    ``cfg`` and check that each connect takes the proc-held route under
    ``held_in`` and is a working stream: the blocking mode asked for,
    close-on-exec, the path reported as its peer (the kernel holds
    ``/proc/self/fd/<fd>/s``, meaningless once the fd is closed) and the
    kernel's addresses readable, the peer is this process, data both ways.
    The place is used (its mtime moved) and empty after each connect, and
    the cwd is unchanged."""
    cwd, cwd_id = os.getcwd(), dir_id(".")
    me = os.getpid()
    with listener_at(p) as listener:
        for req, blocking in ((CONNECT, True), (CONNECT_NONBLOCKING, False)):
            mark_untouched(held_in)
            client, r = _route.run_long(req, p, cfg)
            with client:
                assert r is Route.PROC_HELD, req
                assert client.getblocking() is blocking, req
                assert is_cloexec(client), req
                assert client.getpeername() == os.fsdecode(p), req
                kernel_sockname(client)
                kernel_peername(client)
                server, _ = listener.accept()
                with server:
                    assert peer_pid(server) == me, req
                    ping(server, client)
            assert_used(held_in)
    assert os.getcwd() == cwd
    assert dir_id(".") == cwd_id
    assert listing(parent(p)) == [os.path.basename(p)]
    os.unlink(p)


# ----- reaching the socket -----


# A connect goes through D in the place for D, whatever the place's mode: a
# place others may write to (0777, 0775, 0757, setgid 2777, whose setgid bit
# D inherits and loses again when set to 0700) is used as one of the user's
# own (0700) is, since D is held by its fd and checked after it is opened.
# The connect, blocking or not, reaches the listener at the given path and is
# an ordinary working stream from this process. Nothing is left in the place.
@pytest.mark.parametrize("mode", [0o700, 0o777, 0o775, 0o757, 0o2777])
def test_a_connect_through_d_held_by_its_fd(tmp: TmpDirs, mode: int) -> None:
    p = far_path(tmp)
    held_in = tmp.new()
    os.chmod(held_in, mode)
    check_proc_held(p, fd_cfg([held_in]), held_in)


# The proc-held connect, adopted by asyncio as the standard's is
# (asyncio.open_unix_connection(sock=)), blocking or not: data flows both ways
# and the transport reports the path given as its peer, not the spelling
# through D (whose fd is closed when the connect returns).
def test_a_connect_through_d_is_adopted_by_asyncio(tmp: TmpDirs) -> None:
    p = far_path(tmp)
    held_in = tmp.new()
    cfg = fd_cfg([held_in])

    def connect(req: Request) -> socket.socket:
        client, r = _route.run_long(req, p, cfg)
        assert r is Route.PROC_HELD, req
        return client

    with listener_at(p) as listener:
        asyncio.run(serve_asyncio("proc-held", listener, connect, os.fsdecode(p), served=False))
    assert_empty(held_in)


# What happens inside: while the connect runs, the place holds exactly one
# entry, `sockat.` and 10 letters or digits; it is a directory, owned by the
# effective uid with mode 0700, and holds only `s`, a symlink to the socket
# path as given. The spelling handed to the kernel is `/proc/self/fd/<fd>/s`,
# and that fd is the directory at the name. Afterwards the place is empty.
def test_d_holds_s_pointing_to_the_socket_file(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    p = far_path(tmp)
    base = tmp.new()
    seen: list[bytes] = []
    real = _route.plain_op

    def look_then_connect(req: Request, spelling: str | bytes, *, standard: bool = False) -> socket.socket:
        assert isinstance(spelling, bytes)
        [entry] = os.listdir(base)
        assert re.fullmatch(re.escape(OWN_DIR_PREFIX) + rb"[A-Za-z0-9]{%d}" % RANDOM_LEN, entry), entry
        held = base + b"/" + entry
        st = os.lstat(held)
        assert stat.S_ISDIR(st.st_mode)
        assert stat.S_IMODE(st.st_mode) == 0o700, oct(st.st_mode)
        assert st.st_uid == os.geteuid()
        assert os.listdir(held) == [b"s"]
        assert os.readlink(held + b"/s") == p
        m = re.fullmatch(rb"/proc/self/fd/(\d+)/s", spelling)
        assert m is not None, spelling
        fd = int(m.group(1))
        assert (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (st.st_dev, st.st_ino)
        seen.append(spelling)
        return real(req, spelling, standard=standard)

    with listener_at(p) as listener:
        monkeypatch.setattr(_route, "plain_op", look_then_connect)
        client, r = _route.run_long(CONNECT, p, fd_cfg([base]))
        with client:
            assert r is Route.PROC_HELD
            accept_ping(listener, client)
    assert len(seen) == 1
    assert_empty(base)


# A nonblocking connect through the proc-held route to a full backlog fails at
# once (EAGAIN on Linux; a blocking one would wait for room), and D is removed
# in that failure too.
def test_a_full_backlog_fails_the_nonblocking_connect_at_once_and_leaves_nothing(tmp: TmpDirs) -> None:
    p = far_path(tmp)
    held_in = tmp.new()
    c = fd_cfg([held_in])
    with listener_at(p) as listener:
        listener.listen(1)
        queued: list[socket.socket] = []
        try:
            while True:
                assert len(queued) < 16, "the backlog never filled"
                try:
                    client, r = at_once(lambda: _route.run_long(CONNECT_NONBLOCKING, p, c))
                except BlockingIOError:
                    break
                queued.append(client)
                assert r is Route.PROC_HELD
                assert not client.getblocking()
            assert queued, "nothing got in before it was full"
            mark_untouched(held_in)
            with raises_errno(errno.EAGAIN):
                at_once(lambda: _route.run_long(CONNECT_NONBLOCKING, p, c))[0].close()
            assert_used(held_in)
        finally:
            for q in queued:
                q.close()


# The link's target is the caller's own spelling made absolute against the
# cwd: `s` sits in D, so a relative target would be resolved from there and
# miss the socket. Runs in a child process with the cwd at a temporary
# directory; the relative path is 600 bytes, so no relative fallback.
def test_a_relative_path_is_reached_through_its_absolute_target(tmp: TmpDirs) -> None:
    t = tmp.new()
    held_in = tmp.new()
    p = nest(t, len(t) + 1 + 600, NAME)
    rel = p[len(t) + 1 :]
    with listener_at(p) as listener:
        out = run_child("connect_report", t, {"PATH": rel, "OWN": held_in, "PROC": b"1"})
        assert out["route"] == Route.PROC_HELD.value, out
        listener.setblocking(False)
        listener.accept()[0].close()
    assert_empty(held_in)


# ----- the places -----


# D goes in the first place where it can be made: a place where it cannot
# (missing, a file) is passed over and the next is used; with none the
# connect goes on to the symlink (a safe place given), and with no place for
# the symlink either, ENAMETOOLONG with nothing made.
def test_d_goes_in_the_first_place_where_it_can_be_made(tmp: TmpDirs) -> None:
    t = tmp.new()
    file = t + b"/file"
    with open(file, "wb"):
        pass
    good = tmp.new()
    links = tmp.new()
    p = far_path(tmp)
    check_proc_held(p, fd_cfg([NO_PLACE, file, good]), good)
    with listener_at(p) as listener:
        client, r = _route.run_long(CONNECT, p, fd_cfg([NO_PLACE, file], [links]))
        with client:
            assert r is Route.SYMLINK
            accept_ping(listener, client)
    os.unlink(p)
    assert_empty(links)
    check_unreachable(p, fd_cfg([NO_PLACE, file]))


# Without /proc (no such directory; an empty one) there is no proc-held route: the
# place for D is not touched, and the connect takes the order of the
# systems without /proc (the symlink here, under a safe place).
@pytest.mark.parametrize("empty", [False, True])
def test_without_proc_there_is_no_proc_held_form(tmp: TmpDirs, empty: bool) -> None:
    t = tmp.new()
    none = t + b"/proc/self/fd"
    if empty:
        os.makedirs(none)
    p = far_path(tmp)
    d_place = tmp.new()
    links = tmp.new()
    mark_untouched(d_place)
    with listener_at(p) as listener:
        for req in (CONNECT, CONNECT_NONBLOCKING):
            client, r = _route.run_long(req, p, fd_cfg([d_place], [links], proc=none))
            with client:
                assert r is Route.SYMLINK, req
                accept_ping(listener, client)
    assert_untouched(d_place)
    assert_empty(links)


# When `s` cannot be created in D (here the target is longer than a symlink
# may hold, ENAMETOOLONG), the route is unusable (SPEC: "D か s を作れない")
# and D is removed again. The order is narrowed to proc-held alone, so the
# connect ends in ENAMETOOLONG with proc-held noted as skipped after the
# standard, and no route noted as having failed.
def test_a_symlink_that_cannot_be_created_leaves_nothing(tmp: TmpDirs) -> None:
    base = tmp.new()
    mark_untouched(base)
    cfg = dataclasses.replace(fd_cfg([base]), connect_order=(Route.PROC_HELD,))
    trail = _route.Trail()
    with raises_errno(errno.ENAMETOOLONG):
        _route.reach_long(CONNECT, b"/" + b"x" * 5000, cfg, trail)
    assert [r for r, _ in trail.skipped] == [Route.STANDARD, Route.PROC_HELD]
    assert trail.trying is None
    assert_used(base)


# ----- bind -----


# A bind never takes the proc-held route (`s` is a link to the socket file, which
# a bind does not follow; and the bind's spelling would stay with the
# listener). With a name of 100 bytes no bind route is left: ENAMETOOLONG,
# nothing created in the place or next to the socket; a connect to the same
# path through the same place gets through.
def test_a_bind_does_not_take_the_proc_held_form(tmp: TmpDirs) -> None:
    p = far_path(tmp)
    held_in = tmp.new()
    c = fd_cfg([held_in])
    mark_untouched(held_in)
    before = listing(parent(p))
    with raises_errno(errno.ENAMETOOLONG):
        _route.run_long(BIND, p, c)
    assert listing(parent(p)) == before
    assert_untouched(held_in)
    check_proc_held(p, c, held_in)


# ----- failures -----


# A connect that actually ran through D is the result: a missing socket
# (ENOENT through `s`, which then dangles) and a socket nobody listens on
# (ECONNREFUSED) come back as they are, for both connects, and the call is not
# made again in the next place for D, nor through the symlink (both stay
# untouched). D is removed in the failure too, and nothing appears next to the
# socket.
def test_a_failure_through_d_is_not_retried(tmp: TmpDirs) -> None:
    p = far_path(tmp)
    stale = with_name(p, b"s" * 100)
    stale_socket(with_name(p, b"st.sock"))
    os.rename(with_name(p, b"st.sock"), stale)
    first = tmp.new()
    second = tmp.new()
    links = tmp.new()
    before = listing(parent(p))
    c = fd_cfg([first, second], [links])
    for d in (second, links):
        mark_untouched(d)
    for req in (CONNECT, CONNECT_NONBLOCKING):
        mark_untouched(first)
        with pytest.raises(FileNotFoundError):
            _route.run_long(req, p, c)
        assert_used(first)
        mark_untouched(first)
        with pytest.raises(ConnectionRefusedError):
            _route.run_long(req, stale, c)
        assert_used(first)
    for d in (second, links):
        assert_untouched(d)
    assert listing(parent(p)) == before
