"""The fd-held form of the symlink route (Linux with /proc, connects only):
in a place for the symlink that is not safe, once /proc is confirmed to be
usable, a private directory ``<place>/sockat.XXXXXXXXXX`` (mode 0700) is
made, opened ``O_PATH | O_DIRECTORY | O_NOFOLLOW`` and checked by ``fstat``
(a directory, owned by the effective uid, no permission for group or
others) and set to 0700 through ``/proc/self/fd/<fd>``, a symlink ``s`` to the socket file is created in it through
the fd, and the connect goes to ``/proc/self/fd/<fd>/s`` (the hub's DR-0008).

The socket paths here have a file name of 100 bytes, which does not fit
behind ``/proc/self/fd/<n>/`` (so the /proc route cannot be used), 600 bytes
deep under /tmp (so no relative spelling fits). The thread-cwd route is off
unless a test says otherwise, so a connect that no place carries ends at the
fork route's place, ``ENAMETOOLONG``. No bind route reaches such a name
without the thread-cwd route either, so the listeners are bound under a
short name and renamed (:func:`_support.listener_at`).

A place that is not safe is stood in for by a directory of the test's own
with group or other write and no sticky bit (0777 and the like); the root
runs (docker) also make one owned by another user."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import socket
import stat
import sys
from collections.abc import Callable, Sequence

import pytest
from _support import (
    NO_PLACE,
    NO_PROC,
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
    place,
    raises_errno,
    run_child,
    serve_asyncio,
    stale_socket,
    with_name,
)

from sockat import _route
from sockat._route import BIND, CONNECT, CONNECT_NONBLOCKING, LINK_PREFIX, LINK_RANDOM_LEN, Config, Request, Route, Skip

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the fd-held form needs Linux's /proc")

PROC = b"/proc/self/fd"

NAME = b"n" * 100
"""A file name that does not fit behind ``/proc/self/fd/<n>/`` (16 or 17
bytes of the 107 sun_path holds)."""

NOBODY = 65534
"""Another user, for the root runs (docker's ``nobody``)."""

IS_ROOT = os.geteuid() == 0


def fd_cfg(bases: Sequence[bytes], *, proc: bytes = PROC, thread_cwd: bool = False) -> Config:
    """``bases`` as the places for the symlink, the real /proc unless
    ``proc`` says where else to look, the thread-cwd route off unless asked
    for."""
    return Config(tmp_bases=tuple(bases), proc_self_fd=proc, thread_cwd=thread_cwd)


def far_path(tmp: TmpDirs, name: bytes = NAME) -> bytes:
    """A socket path 600 bytes deep under /tmp with the file name ``name``,
    far from the cwd (no relative spelling fits)."""
    p = nest(tmp.new(), 600, name)
    assert_far_from_cwd(p)
    return p


def unsafe_place(tmp: TmpDirs, mode: int = 0o777) -> bytes:
    """A place of the test's own with exactly ``mode``, which must not count
    as safe (another user could rename or replace entries in it)."""
    d = tmp.new()
    os.chmod(d, mode)
    assert not _route.safe_base(d), oct(mode)
    return d


def safe_place(tmp: TmpDirs) -> bytes:
    """A place of the test's own with mode 0700, which counts as safe."""
    d = tmp.new()
    assert _route.safe_base(d)
    return d


def assert_used(d: bytes) -> None:
    """Something was created in ``d`` and removed again since
    :func:`_support.mark_untouched` (its mtime moved), and nothing is left."""
    assert os.stat(d).st_mtime != UNTOUCHED, f"nothing was made in {d!r}"
    assert_empty(d)


def check_fd_held(p: bytes, cfg: Config, held_in: bytes) -> None:
    """Connect (blocking and nonblocking) to a listener at ``p`` through
    ``cfg`` and check that each connect takes the fd-held form under
    ``held_in`` and is a working stream: the blocking mode asked for,
    close-on-exec, the path reported as its peer (the kernel holds
    ``/proc/self/fd/<fd>/s``, meaningless once the fd is closed) and the
    kernel's addresses readable, the peer is this process, data
    both ways. The place is used (its mtime moved) and empty after each
    connect, and the cwd is unchanged."""
    cwd, cwd_id = os.getcwd(), dir_id(".")
    me = os.getpid()
    with listener_at(p) as listener:
        for req, blocking in ((CONNECT, True), (CONNECT_NONBLOCKING, False)):
            mark_untouched(held_in)
            client, r = _route.run_long(req, p, cfg)
            with client:
                assert r is Route.FD_HELD, req
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
    os.unlink(p)


# ----- reaching the socket -----


# A place that is not safe (here the test's own, writable by group or others,
# not sticky) is not passed over for a connect on Linux with /proc: the
# connect, blocking or not, goes through a private directory made there and
# held by its fd, reaches the listener at the given path and is an ordinary
# working stream from this process. Nothing is left in the place afterwards
# (neither the `sockat.*` directory nor `s`), though something was made in it.
# A setgid place (2777) passes its setgid bit on to the directory made in it;
# that is no permission for group or others, so the directory is used too.
@pytest.mark.parametrize("mode", [0o777, 0o775, 0o757, 0o2777])
def test_a_connect_through_an_unsafe_place_holds_a_private_directory_by_its_fd(tmp: TmpDirs, mode: int) -> None:
    p = far_path(tmp)
    held_in = unsafe_place(tmp, mode)
    check_fd_held(p, fd_cfg([held_in]), held_in)


# The fd-held connect, adopted by asyncio as the standard's is
# (asyncio.open_unix_connection(sock=)), blocking or not: data flows both ways
# and the transport reports the path given as its peer, not the spelling
# through the held directory (whose fd is closed when the connect returns).
# The listener is bound under a short name and renamed (no bind route reaches
# the 100-byte name without the thread-cwd route), so only the client's side
# is checked here; the bind routes are put through asyncio by check_route.
def test_a_connect_through_the_held_directory_is_adopted_by_asyncio(tmp: TmpDirs) -> None:
    p = far_path(tmp)
    held_in = unsafe_place(tmp)
    cfg = fd_cfg([held_in])

    def connect(req: Request) -> socket.socket:
        client, r = _route.run_long(req, p, cfg)
        assert r is Route.FD_HELD, req
        return client

    with listener_at(p) as listener:
        asyncio.run(serve_asyncio("fd-held", listener, connect, os.fsdecode(p), served=False))
    assert_empty(held_in)


# Run as root (docker), a sticky place owned by another user is not safe either
# (its owner may remove or rename any entry), and the connect holds a private
# directory there the same way. Other users cannot chown, so this is skipped
# for them.
@pytest.mark.skipif(not IS_ROOT, reason="only root can make a place owned by another user")
def test_another_users_sticky_place_is_used_through_the_fd(tmp: TmpDirs) -> None:
    p = far_path(tmp)
    theirs = tmp.new()
    os.chmod(theirs, 0o1777)
    os.chown(theirs, NOBODY, NOBODY)
    assert not _route.safe_base(theirs)
    check_fd_held(p, fd_cfg([theirs]), theirs)


# What happens inside: while the connect runs, the place holds exactly one
# entry, `sockat.` and 10 letters or digits (the name of the symlink route); it
# is a directory, not a symlink, owned by the effective uid with mode 0700, and
# holds only `s`, a symlink to the socket path as given. The spelling handed
# to the kernel is `/proc/self/fd/<fd>/s`, and that fd is the directory at the
# name. Afterwards the place is empty. In a setgid place (2777) the bit the
# directory inherits is gone by then: it was set to exactly 0700.
@pytest.mark.parametrize("base_mode", [0o777, 0o2777])
def test_the_held_directory_is_private_and_s_points_to_the_socket_file(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, base_mode: int
) -> None:
    p = far_path(tmp)
    base = unsafe_place(tmp, base_mode)
    seen: list[bytes] = []
    real = _route.plain_op

    def look_then_connect(req: Request, spelling: str | bytes) -> socket.socket:
        assert isinstance(spelling, bytes)
        [entry] = os.listdir(base)
        assert re.fullmatch(re.escape(LINK_PREFIX) + rb"[A-Za-z0-9]{%d}" % LINK_RANDOM_LEN, entry), entry
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
        assert os.fstat(fd).st_ino == st.st_ino
        assert os.fstat(fd).st_dev == st.st_dev
        seen.append(spelling)
        return real(req, spelling)

    with listener_at(p) as listener:
        monkeypatch.setattr(_route, "plain_op", look_then_connect)
        out = _route.fd_held_route(CONNECT, base, p, PROC)
        assert isinstance(out, tuple)
        client, r = out
        with client:
            assert r is Route.FD_HELD
            accept_ping(listener, client)
    assert len(seen) == 1
    assert_empty(base)


# A nonblocking connect through the fd-held form to a full backlog fails at
# once (EAGAIN on Linux; a blocking one would wait for room), and the held
# directory is removed in that failure too.
def test_a_full_backlog_fails_the_nonblocking_connect_at_once_and_leaves_nothing(tmp: TmpDirs) -> None:
    p = far_path(tmp)
    held_in = unsafe_place(tmp)
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
                assert r is Route.FD_HELD
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
# cwd: `s` sits in the held directory, so a relative target would be resolved
# from there and miss the socket. Runs in a child process with the cwd at a
# temporary directory; the relative path is 600 bytes, so no relative route.
def test_a_relative_path_is_reached_through_its_absolute_target(tmp: TmpDirs) -> None:
    t = tmp.new()
    held_in = unsafe_place(tmp)
    p = nest(t, len(t) + 1 + 600, NAME)
    rel = p[len(t) + 1 :]
    with listener_at(p) as listener:
        out = run_child("connect_report", t, {"PATH": rel, "BASE": held_in})
        assert out["route"] == Route.FD_HELD.value, out
        listener.setblocking(False)
        listener.accept()[0].close()
    assert_empty(held_in)


# The directory is made with mode 0700, which a umask may cut down. A umask
# that takes owner bits off (0o277: no write, the directory comes out 0500;
# 0o477: no read, 0300; 0o777: nothing, 0000) leaves no permission for group
# or others, so the check passes: the directory is opened O_PATH, which needs
# no permission on it, and set to 0700 through /proc before `s` is created in
# it. The connect gets through and nothing is left (no `sockat.*` piling up,
# one per connect). Root passes over permissions, so the runs as another user
# (docker's nobody) are the ones that show the open and the chmod are needed.
# A umask that takes nothing off the owner (0o077, 0o022) changes nothing.
# Runs in a child process because the umask is process-wide.
@pytest.mark.parametrize("umask", ["277", "477", "777", "077", "022"])
def test_a_umask_taking_owner_bits_off_does_not_stop_the_fd_held_form(tmp: TmpDirs, umask: str) -> None:
    held_in = unsafe_place(tmp)
    p = far_path(tmp)
    with listener_at(p) as listener:
        out = run_child("connect_report", b"/", {"PATH": p, "BASE": held_in, "UMASK": umask.encode()})
        assert out["route"] == Route.FD_HELD.value, out
        listener.setblocking(False)
        listener.accept()[0].close()
    assert_empty(held_in)


# The same within this process, by a make_dir that leaves the directory short
# of owner bits as such a umask would (0500, 0300, 0000): by the time of the
# connect the directory is 0700.
@pytest.mark.parametrize("short", [0o500, 0o300, 0o000])
def test_a_directory_short_of_owner_bits_is_set_to_0700(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, short: int
) -> None:
    p = far_path(tmp)
    base = unsafe_place(tmp)
    modes: list[int] = []
    real = _route.plain_op

    def look_then_connect(req: Request, spelling: str | bytes) -> socket.socket:
        [entry] = os.listdir(base)
        modes.append(stat.S_IMODE(os.lstat(base + b"/" + entry).st_mode))
        return real(req, spelling)

    def make_dir(d: bytes, mode: int) -> None:
        os.mkdir(d, mode)
        os.chmod(d, short)

    with listener_at(p) as listener:
        monkeypatch.setattr(_route, "plain_op", look_then_connect)
        out = _route.fd_held_route(CONNECT, base, p, PROC, make_dir=make_dir)
        assert isinstance(out, tuple), out
        client, r = out
        with client:
            assert r is Route.FD_HELD
            accept_ping(listener, client)
    assert modes == [0o700]
    assert_empty(base)


# ----- bind -----


# A bind never takes the fd-held form: its spelling would be
# `/proc/self/fd/<fd>/s/<name>`, longer than the /proc route's
# `/proc/self/fd/<fd>/<name>`, which is tried first, so it would carry no name
# the /proc route does not. With only an unsafe place a name that does not
# fit behind /proc (95, 100 bytes) is ENAMETOOLONG for the bind, and nothing is
# created in the place or next to the socket; a connect to the same path
# through the same place gets through.
@pytest.mark.parametrize("n", [95, 100])
def test_a_bind_does_not_take_the_fd_held_form(tmp: TmpDirs, n: int) -> None:
    p = far_path(tmp, b"b" * n)
    held_in = unsafe_place(tmp)
    c = fd_cfg([held_in])
    mark_untouched(held_in)
    before = listing(parent(p))
    with raises_errno(errno.ENAMETOOLONG):
        _route.run_long(BIND, p, c)
    assert listing(parent(p)) == before
    assert_untouched(held_in)
    check_fd_held(p, c, held_in)


# ----- the order of the places -----


# TMPDIR is used up before /tmp, whichever form it takes: an unsafe first
# place takes the fd-held form even though the second is safe (a symlink could
# go there), a safe first place takes a plain symlink and the unsafe second is
# never touched, and of two unsafe places only the first is used.
def test_the_first_place_is_used_in_whichever_form_it_takes(tmp: TmpDirs) -> None:
    p = far_path(tmp)
    with listener_at(p) as listener:
        for first_safe, second_safe, want in (
            (False, True, Route.FD_HELD),
            (True, False, Route.SYMLINK_TO_FILE),
            (False, False, Route.FD_HELD),
        ):
            first = safe_place(tmp) if first_safe else unsafe_place(tmp)
            second = safe_place(tmp) if second_safe else unsafe_place(tmp)
            mark_untouched(first)
            mark_untouched(second)
            client, r = _route.run_long(CONNECT, p, fd_cfg([first, second]))
            with client:
                assert r is want, (first_safe, second_safe)
                accept_ping(listener, client)
            assert_used(first)
            assert_untouched(second)


# The same through the real environment (Config.from_env, as the public API
# reads it): the places are $TMPDIR, then /tmp (safe: sticky and root's), and
# TMPDIR is used first. An unsafe TMPDIR takes the fd-held form there (/tmp
# would have taken a symlink); a safe one takes a symlink there. Runs in a
# child process because it sets TMPDIR.
@pytest.mark.parametrize("safe", [False, True])
def test_from_the_environment_tmpdir_is_used_before_tmp(tmp: TmpDirs, safe: bool) -> None:
    assert _route.safe_base(b"/tmp"), "the premise: /tmp is a safe place here"
    p = far_path(tmp)
    tmpdir = safe_place(tmp) if safe else unsafe_place(tmp)
    mark_untouched(tmpdir)
    with listener_at(p) as listener:
        out = run_child("connect_report", b"/", {"PATH": p}, env={"TMPDIR": tmpdir})
        want = Route.SYMLINK_TO_FILE if safe else Route.FD_HELD
        assert out["route"] == want.value, out
        assert out["bases"] == json.dumps([os.fsdecode(tmpdir), "/tmp"]), out
        listener.setblocking(False)
        listener.accept()[0].close()
    assert_used(tmpdir)


# ----- without /proc -----


# Without /proc (no such directory; an empty one) there is no fd-held form: an
# unsafe place is passed over without creating anything in it (its mtime is
# unchanged), as on the systems without /proc, and the next, safe place takes
# a symlink. With only the unsafe place nothing reaches the path: ENAMETOOLONG
# for every request, nothing made.
@pytest.mark.parametrize("empty", [False, True])
def test_without_proc_an_unsafe_place_is_passed_over(tmp: TmpDirs, empty: bool) -> None:
    t = tmp.new()
    none = t + b"/proc/self/fd"
    if empty:
        os.makedirs(none)
    p = far_path(tmp)
    bad = unsafe_place(tmp)
    good = safe_place(tmp)
    mark_untouched(bad)
    with listener_at(p) as listener:
        for req in (CONNECT, CONNECT_NONBLOCKING):
            client, r = _route.run_long(req, p, fd_cfg([bad, good], proc=none))
            with client:
                assert r is Route.SYMLINK_TO_FILE, req
                accept_ping(listener, client)
    assert_untouched(bad)
    assert_empty(good)
    os.unlink(p)
    check_unreachable(p, fd_cfg([bad], proc=none), [bad])


# The form itself confirms that /proc can be used (through an fd of the
# place) before it makes anything, whoever calls it: called directly without
# /proc (no such directory; an empty one), it is unusable and the place is
# untouched.
@pytest.mark.parametrize("empty", [False, True])
def test_without_proc_the_fd_held_form_makes_nothing(tmp: TmpDirs, empty: bool) -> None:
    t = tmp.new()
    none = NO_PROC
    if empty:
        none = t + b"/proc/self/fd"
        os.makedirs(none)
    p = far_path(tmp)
    base = unsafe_place(tmp)
    mark_untouched(base)
    assert _route.fd_held_route(CONNECT, base, p, none) is Skip.UNUSABLE
    assert_untouched(base)


# Should the held directory's own fd not be found under /proc (the place's
# was), the form is unusable before `s` is created, and the directory made
# for it is removed again. The second lookup is made to fail by a stand-in.
def test_a_held_directory_not_found_under_proc_is_removed_again(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    p = far_path(tmp)
    base = unsafe_place(tmp)
    real = _route.proc_fd_dir
    looked: list[int] = []

    def first_only(fd: int, proc_self_fd: bytes) -> bytes | None:
        looked.append(fd)
        return real(fd, proc_self_fd) if len(looked) == 1 else None

    monkeypatch.setattr(_route, "proc_fd_dir", first_only)
    mark_untouched(base)
    assert _route.fd_held_route(CONNECT, base, p, PROC) is Skip.UNUSABLE
    assert len(looked) == 2
    assert_used(base)


# ----- a replaced directory -----


REPLACEMENTS = ["mode-755", "mode-750", "mode-701", "renamed", "renamed-holding-s", "symlink", "owner"]


def replacing_make_dir(kind: str, aside: bytes) -> Callable[[bytes, int], None]:
    """A ``make_dir`` for :func:`_route.fd_held_route` that makes the
    directory and then, before the route opens it, changes what is at the
    name, as another user (or root) could in an unsafe place:

    * ``mode-<octal>``: its mode becomes that (with some permission for
      group or others);
    * ``owner``: it is given to another user (root only);
    * ``renamed``: it is renamed to ``<aside>/orig`` and an empty directory
      of mode 0777 takes its name;
    * ``renamed-holding-s``: the same, the new directory holding a file
      ``s`` of its own;
    * ``symlink``: it is renamed to ``<aside>/orig`` and a symlink to
      ``<aside>/decoy`` (a directory of the user's own, mode 0700, which
      would pass the check if the symlink were followed) takes its name.
    """

    def make(d: bytes, mode: int) -> None:
        os.mkdir(d, mode)
        if kind.startswith("mode-"):
            os.chmod(d, int(kind[len("mode-") :], 8))
            return
        if kind == "owner":
            os.chown(d, NOBODY, NOBODY)
            return
        os.rename(d, aside + b"/orig")
        if kind == "symlink":
            decoy = place(aside, b"decoy", 0o700)
            mark_untouched(decoy)
            os.symlink(decoy, d)
            return
        place(parent(d), os.path.basename(d), 0o777)
        if kind == "renamed-holding-s":
            with open(d + b"/s", "wb") as f:
                f.write(b"theirs")

    return make


def skip_unless_root_for(kind: str) -> None:
    if kind == "owner" and not IS_ROOT:
        pytest.skip("only root can give a directory to another user")


# A directory replaced between its creation and the open is not used: the
# check on the opened fd (a directory, not a symlink, owned by the effective
# uid, no permission for group or others) fails, or the open itself
# (O_NOFOLLOW on a symlink), and the
# form is unusable before any `s` is created or any connect made (a listener
# at the path sees nothing). A directory that failed the check may be someone
# else's, so it is left alone, contents and all (SPEC: removed only when it
# passed the check). What is left:
#
# * mode (any bit for group or others: 0755, 0750, 0701), owner: the
#   directory at the name, as the replacement left it;
# * renamed: the replacement at the name, empty as it was put there; the one
#   made is out of reach at its new name and stays there;
# * renamed-holding-s: the same, and the `s` this call did not create is left
#   as it is;
# * symlink: nothing is opened, so nothing is touched: the symlink stays, and
#   the directory it points to is not used (its mtime is unchanged).
@pytest.mark.parametrize("kind", REPLACEMENTS)
def test_a_directory_replaced_before_it_is_opened_is_not_used(tmp: TmpDirs, kind: str) -> None:
    skip_unless_root_for(kind)
    p = far_path(tmp)
    base = unsafe_place(tmp)
    aside = tmp.new()
    with listener_at(p) as listener:
        listener.setblocking(False)
        out = _route.fd_held_route(CONNECT, base, p, PROC, make_dir=replacing_make_dir(kind, aside))
        assert out is Skip.UNUSABLE, out
        with pytest.raises(BlockingIOError):
            listener.accept()
    left = listing(base)
    if kind.startswith("mode-") or kind in ("owner", "renamed"):
        [entry] = left
        assert stat.S_ISDIR(os.lstat(base + b"/" + entry).st_mode)
        assert listing(base + b"/" + entry) == []
    elif kind == "renamed-holding-s":
        [entry] = left
        assert listing(base + b"/" + entry) == [b"s"]
        with open(base + b"/" + entry + b"/s", "rb") as f:
            assert f.read() == b"theirs"
    else:
        [entry] = left
        assert os.readlink(base + b"/" + entry) == aside + b"/decoy"
        assert listing(aside + b"/decoy") == []
        assert_untouched(aside + b"/decoy")
    if kind.startswith("renamed") or kind == "symlink":
        assert listing(aside + b"/orig") == []


# Through the route order: a place whose directory was replaced before the
# open counts as unusable, the next place is used (the fd-held form again,
# there), and with no other place the connect is ENAMETOOLONG without
# reaching the listener.
@pytest.mark.parametrize("kind", ["mode-755", "renamed", "symlink", "owner"])
def test_a_place_whose_directory_is_replaced_is_passed_over(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    skip_unless_root_for(kind)
    p = far_path(tmp)
    first = unsafe_place(tmp)
    second = unsafe_place(tmp)
    real = _route.fd_held_route
    replaced: list[bytes] = []

    def held(
        req: Request,
        base: bytes,
        path: bytes,
        proc_self_fd: bytes,
        make_dir: Callable[[bytes, int], None] = os.mkdir,
    ) -> tuple[socket.socket, Route] | Skip:
        if base == first:
            aside = tmp.new()
            replaced.append(aside)
            make_dir = replacing_make_dir(kind, aside)
        return real(req, base, path, proc_self_fd, make_dir)

    with listener_at(p) as listener:
        monkeypatch.setattr(_route, "fd_held_route", held)
        mark_untouched(second)
        client, r = _route.run_long(CONNECT, p, fd_cfg([first, second]))
        with client:
            assert r is Route.FD_HELD
            accept_ping(listener, client)
        assert_used(second)
        listener.setblocking(False)
        with raises_errno(errno.ENAMETOOLONG):
            _route.run_long(CONNECT, p, fd_cfg([first]))
        with pytest.raises(BlockingIOError):
            listener.accept()
    assert len(replaced) == 2


# Once the directory is open, the name no longer matters: renamed away and
# replaced (by someone else's directory) while the connect runs, the connect
# still goes through the fd to the directory made, and reaches the socket.
# Afterwards `s` is removed through the fd (the directory made, now out of
# reach under another name, is left empty), and the replacement at the name
# is not removed: the name no longer refers to the fd's directory.
def test_a_directory_replaced_after_it_is_opened_changes_nothing(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    p = far_path(tmp)
    base = unsafe_place(tmp)
    aside = tmp.new()
    real = _route.plain_op
    moved: list[bytes] = []

    def replace_then_connect(req: Request, spelling: str | bytes) -> socket.socket:
        [entry] = os.listdir(base)
        os.rename(base + b"/" + entry, aside + b"/" + entry)
        place(base, entry, 0o777)
        moved.append(entry)
        return real(req, spelling)

    with listener_at(p) as listener:
        monkeypatch.setattr(_route, "plain_op", replace_then_connect)
        out = _route.fd_held_route(CONNECT, base, p, PROC)
        assert isinstance(out, tuple)
        client, r = out
        with client:
            assert r is Route.FD_HELD
            accept_ping(listener, client)
    [entry] = moved
    assert listing(base) == [entry]
    assert listing(base + b"/" + entry) == []
    assert listing(aside) == [entry]
    assert listing(aside + b"/" + entry) == []


# ----- the directory or the symlink cannot be made -----


# The directory's name is drawn as the symlink route's is: a name already
# taken (by anything; here a symlink) is skipped for the next one and the
# existing entry is left as it was; after 16 taken names the place is
# unusable and nothing is created in it. The names come from a stand-in
# generator so the collision is certain.
def test_a_taken_name_is_skipped_and_16_taken_names_end_the_attempt(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = far_path(tmp)
    base = unsafe_place(tmp)
    taken = LINK_PREFIX + b"A" * LINK_RANDOM_LEN
    free = LINK_PREFIX + b"B" * LINK_RANDOM_LEN
    os.symlink(b"/nonexistent", base + b"/" + taken)
    drawn: list[bytes] = []

    def names(*picks: bytes) -> Callable[[], bytes]:
        it = iter(picks)

        def next_name() -> bytes:
            n = next(it, taken)
            drawn.append(n)
            return n

        return next_name

    with listener_at(p) as listener:
        monkeypatch.setattr(_route, "random_name", names(taken, free))
        out = _route.fd_held_route(CONNECT, base, p, PROC)
        assert isinstance(out, tuple)
        client, r = out
        with client:
            assert r is Route.FD_HELD
            accept_ping(listener, client)
    assert drawn == [taken, free]
    assert listing(base) == [taken]
    assert os.readlink(base + b"/" + taken) == b"/nonexistent"

    drawn.clear()
    monkeypatch.setattr(_route, "random_name", names())
    mark_untouched(base)
    assert _route.fd_held_route(CONNECT, base, p, PROC) is Skip.UNUSABLE
    assert len(drawn) == 16
    assert_untouched(base)
    assert listing(base) == [taken]


# A place where the directory cannot be made (it does not exist; it is a file;
# it is a directory others may write to but its owner, the user, may not) is
# unusable rather than an error, the next place is used, and with no other
# place the connect is ENAMETOOLONG with nothing made. Root makes a directory
# whatever the permissions, so the last case runs only for other users (the
# docker nobody run).
@pytest.mark.parametrize("what", ["missing", "file", "unwritable"])
def test_a_place_where_the_directory_cannot_be_made_is_passed_over(tmp: TmpDirs, what: str) -> None:
    if what == "unwritable" and IS_ROOT:
        pytest.skip("root makes a directory whatever the permissions")
    t = tmp.new()
    if what == "missing":
        bad = NO_PLACE
    elif what == "file":
        bad = t + b"/file"
        with open(bad, "wb"):
            pass
        os.chmod(bad, 0o666)
    else:
        bad = place(t, b"unwritable", 0o577)
    assert not _route.safe_base(bad), what
    untouched = [bad] if what == "unwritable" else []
    for d in untouched:
        mark_untouched(d)
    good = unsafe_place(tmp)
    p = far_path(tmp)
    with listener_at(p) as listener:
        assert _route.fd_held_route(CONNECT, bad, p, PROC) is Skip.UNUSABLE
        mark_untouched(good)
        client, r = _route.run_long(CONNECT, p, fd_cfg([bad, good]))
        with client:
            assert r is Route.FD_HELD
            accept_ping(listener, client)
    assert_used(good)
    for d in untouched:
        assert_untouched(d)
        assert listing(d) == []
    os.unlink(p)
    check_unreachable(p, fd_cfg([bad]), untouched)


# When `s` cannot be created in the held directory (here the target is longer
# than a symlink may hold, ENAMETOOLONG), the form is unusable and the
# directory is removed again.
def test_a_symlink_that_cannot_be_created_leaves_nothing(tmp: TmpDirs) -> None:
    base = unsafe_place(tmp)
    mark_untouched(base)
    assert _route.fd_held_route(CONNECT, base, b"/" + b"x" * 5000, PROC) is Skip.UNUSABLE
    assert_used(base)


# ----- failures -----


# A connect that actually ran through the held directory is the result: a
# missing socket (ENOENT through `s`, which then dangles) and a socket nobody
# listens on (ECONNREFUSED) come back as they are, for both connects, and the
# call is not made again under the next place, safe or not (it stays
# untouched), nor on the thread-cwd route (on here). The held directory is
# removed in the failure case too, and nothing appears next to the socket.
def test_a_failure_through_the_held_directory_is_not_retried(tmp: TmpDirs) -> None:
    p = far_path(tmp)
    stale = with_name(p, b"s" * 100)
    stale_socket(with_name(p, b"st.sock"))
    os.rename(with_name(p, b"st.sock"), stale)
    first = unsafe_place(tmp)
    before = listing(parent(p))
    for second in (unsafe_place(tmp), safe_place(tmp)):
        c = fd_cfg([first, second], thread_cwd=True)
        mark_untouched(second)
        for req in (CONNECT, CONNECT_NONBLOCKING):
            mark_untouched(first)
            with pytest.raises(FileNotFoundError):
                _route.run_long(req, p, c)
            assert_used(first)
            mark_untouched(first)
            with pytest.raises(ConnectionRefusedError):
                _route.run_long(req, stale, c)
            assert_used(first)
        assert_untouched(second)
    assert listing(parent(p)) == before
