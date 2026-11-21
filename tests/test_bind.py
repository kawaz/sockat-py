"""The routes of a bind to a long path (hub SPEC "bind の順", SPEC-routes
"経路"): link-dir by procB and by B's real path, link-file, thread-cwd / at,
each forced by the environment it is given; what the spelling the kernel
keeps is good for (``Spelling``) and that it never names an unrelated file;
failures that are the outcome and not tried again."""

from __future__ import annotations

import errno
import os
import socket
import threading

import pytest
from _support import (
    HAS_PROC,
    HAS_THREAD_ROUTE,
    NAME_MAX_THREAD_CWD,
    NO_PROC,
    THREAD_CWD,
    THREAD_ROUTE_IN_BIND_ROW,
    TmpDirs,
    accept_ping,
    base_of,
    bind_in,
    connect_in,
    deep_dir,
    ds,
    env_at,
    is_socket,
    kernel_sockname,
    listing,
    long_path,
    raises_errno,
    run_child,
    standard_errno,
)

import sockat.ex
from sockat import _calls
from sockat._place import Env, LinkFile
from sockat._spelling import Trail, Until
from sockat._sys import DIRECT_MAX, IS_APPLE, IS_FREEBSD, IS_LINUX

THREAD_ROUTE = "at" if IS_FREEBSD else "thread-cwd"
FOLLOWS = LinkFile.FOLLOWS


def entries(env: Env) -> list[bytes]:
    """What is in the process's D in ``env`` (its E)."""
    b = env.auto_base()
    assert b is not None
    d = b.dir(env.config.uid)
    assert d is not None
    return sorted(os.listdir(b.real + b"/" + d.name))


def check_served(listener: socket.socket, path: bytes, env: Env) -> None:
    """The socket is at ``path`` (not elsewhere) and a connect there reaches
    it."""
    assert is_socket(path)
    client, _ = connect_in(env, path)
    with client:
        accept_ping(listener, client)


# Where /proc can be used, a bind goes by link-dir through procB:
# /proc/<pid>/fd/<B's fd>/D/E/<name>, E a symlink in D to the socket's
# directory. The spelling names the path until the process ends, and only to
# this uid; it does not go through /tmp.
@pytest.mark.skipif(not HAS_PROC, reason="/proc is Linux's")
def test_link_dir_through_procb(tmp: TmpDirs) -> None:
    env = env_at(tmp.new())
    p = long_path(tmp.new())
    trail = Trail()
    done = bind_in(env, p, trail=trail)
    with done.sock as listener:
        assert done.route == "link-dir"
        assert done.base == "proc"
        assert done.spelling == sockat.ex.Linked(
            removable=True, until=Until.PROCESS, usable_cross_uid=False, usable_with_private_tmp=True
        )
        assert [s.name for s in trail.skipped] == ["standard"]
        spelling = os.fsencode(kernel_sockname(listener))
        b = env.auto_base()
        assert b is not None
        assert spelling.startswith(b"/proc/%d/fd/%d/" % (os.getpid(), b.fd))
        assert spelling.endswith(b"/s.sock")
        assert os.path.samefile(spelling, p)
        check_served(listener, p, env)
        assert len(entries(env)) == 1


# Without /proc, link-dir goes through B's real path: <B>/D/E/<name>. It
# names the path until cleanup; another uid can follow it when every
# directory on the way lets others search it.
def test_link_dir_through_bs_real_path(tmp: TmpDirs) -> None:
    place = tmp.new()
    os.chmod(place, 0o755)
    env = env_at(place, proc=NO_PROC)
    p = long_path(tmp.new())
    done = bind_in(env, p)
    with done.sock as listener:
        assert done.route == "link-dir"
        assert done.base == "tmp"
        assert isinstance(done.spelling, sockat.ex.Linked)
        assert done.spelling.removable
        assert done.spelling.until is Until.CLEANUP
        assert done.spelling.usable_cross_uid
        spelling = os.fsencode(kernel_sockname(listener))
        assert spelling.startswith(base_of(env) + b"/")
        check_served(listener, p, env)
        # removable: unlinking by the spelling removes the socket file.
        os.unlink(spelling)
        assert not os.path.lexists(p)


# B under a directory others cannot search (0700): the spelling cannot be
# followed by another uid, and says so.
def test_a_spelling_others_cannot_follow_says_so(tmp: TmpDirs) -> None:
    env = env_at(tmp.new(), proc=NO_PROC)
    done = bind_in(env, long_path(tmp.new()))
    done.sock.close()
    assert isinstance(done.spelling, sockat.ex.Linked)
    assert not done.spelling.usable_cross_uid


# Where a bind was measured to follow a symlink in the last component
# (macOS, NetBSD), link-file reaches a file name too long for link-dir:
# <B>/D/E, E a symlink to the socket file. Unlinking by the spelling removes
# E, not the file (removable false). B is under /tmp: a private /tmp hides it.
@pytest.mark.skipif(not (IS_APPLE or os.uname().sysname == "NetBSD"), reason="a bind follows the symlink here only")
def test_link_file_where_a_bind_follows_the_symlink(tmp: TmpDirs) -> None:
    env = env_at(tmp.new())
    p = long_path(tmp.new(), name=b"n" * 100)
    trail = Trail()
    done = bind_in(env, p, trail=trail)
    with done.sock as listener:
        assert done.route == "link-file"
        assert [s.name for s in trail.skipped] == ["standard", "link-dir"]
        assert done.spelling == sockat.ex.Linked(
            removable=False, until=Until.CLEANUP, usable_cross_uid=False, usable_with_private_tmp=False
        )
        check_served(listener, p, env)
        spelling = os.fsencode(kernel_sockname(listener))
        os.unlink(spelling)
        assert is_socket(p)


# On a system not measured, a bind tells at run time whether the kernel
# followed link-file's symlink: when the bind fails and nothing is at the
# path, it was not followed: link-file is skipped, its E removed, and the
# next route tried. Forced on Linux (which does not follow it).
@pytest.mark.skipif(not IS_LINUX, reason="a system whose bind does not follow the symlink")
def test_link_file_told_at_run_time_not_followed(tmp: TmpDirs) -> None:
    env = env_at(tmp.new(), proc=NO_PROC, link_file=LinkFile.TOLD_AT_RUN_TIME, thread_cwd=False)
    p = long_path(tmp.new(), name=b"n" * 100)
    trail = Trail()
    with raises_errno(errno.ENAMETOOLONG):
        bind_in(env, p, trail=trail)
    assert [(s.name, s.reason) for s in trail.skipped][2] == ("link-file", "symlink not followed by the bind")
    assert entries(env) == []
    assert not os.path.lexists(p)


# ... and when something is at the path, the failure is the outcome
# (the standard's errno for a file at the path), raised from link-file, with no route tried after it.
@pytest.mark.skipif(not IS_LINUX, reason="a system whose bind does not follow the symlink")
def test_link_file_told_at_run_time_with_a_file_there(tmp: TmpDirs) -> None:
    env = env_at(tmp.new(), proc=NO_PROC, link_file=LinkFile.TOLD_AT_RUN_TIME)
    p = long_path(tmp.new(), name=b"n" * 100)
    with open(p, "wb"):
        pass
    trail = Trail()
    with raises_errno(standard_errno("bind-exists")):
        bind_in(env, p, trail=trail)
    assert trail.trying == "link-file"
    assert [s.name for s in trail.skipped] == ["standard", "link-dir"]
    assert entries(env) == []


# Where the spellings through B do not fit, thread-cwd (at on FreeBSD) binds
# E/<name>, E a random symlink in D to the socket's directory, from a
# throwaway thread whose cwd is D, and removes E at once. Neither the
# process's cwd nor the calling thread's changes; nothing is left in D; the
# socket is at the path. The spelling names nothing afterwards.
@pytest.mark.skipif(not HAS_THREAD_ROUTE, reason="no thread cwd / bindat here")
def test_thread_cwd_or_at(tmp: TmpDirs) -> None:
    place = deep_dir(tmp.new(), 90)
    env = env_at(place, proc=NO_PROC, link_file=LinkFile.NOT_TRIED)
    p = long_path(tmp.new())
    cwd = os.getcwd()
    trail = Trail()
    done = bind_in(env, p, trail=trail)
    with done.sock as listener:
        assert os.getcwd() == cwd
        assert done.route == THREAD_ROUTE
        assert done.base is None
        assert done.spelling == sockat.ex.Dangling()
        assert [s.name for s in trail.skipped] == ["standard", "link-dir"]
        assert entries(env) == []
        spelling = os.fsencode(kernel_sockname(listener))
        assert spelling.endswith(b"/s.sock")
        assert not spelling.startswith(b"/")
        check_served(listener, p, env)
    # The calling thread's own cwd, on a thread that is not the main one.
    seen: list[str] = []

    def other() -> None:
        before = os.getcwd()
        q = long_path(tmp.new())
        bind_in(env, q).sock.close()
        seen.extend([before, os.getcwd()])

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert seen[0] == seen[1] == cwd


# The longest file name thread-cwd / at bind is sockaddr less "E/" (E 10
# bytes); one more is ENAMETOOLONG, with nothing made in D.
@pytest.mark.skipif(not HAS_THREAD_ROUTE, reason="no thread cwd / bindat here")
@pytest.mark.parametrize("extra", [0, 1])
def test_thread_cwd_reaches_its_longest_name(tmp: TmpDirs, extra: int) -> None:
    place = deep_dir(tmp.new(), 90)
    env = env_at(place, proc=NO_PROC, link_file=LinkFile.NOT_TRIED)
    name = b"n" * (NAME_MAX_THREAD_CWD + extra)
    p = long_path(tmp.new(), name=name, total=len(name) + 60)
    if extra:
        trail = Trail()
        with raises_errno(errno.ENAMETOOLONG):
            bind_in(env, p, trail=trail)
        assert [s.name for s in trail.skipped] == ["standard", "link-dir", THREAD_ROUTE]
        assert trail.trying is None
        assert ds(base_of(env)) == []
    else:
        done = bind_in(env, p)
        with done.sock as listener:
            assert done.route == THREAD_ROUTE
            check_served(listener, p, env)


# A bind's real failure is the outcome, raised with the route that failed,
# and no route after it is tried (a file already at the path: the standard's
# errno for it). The E an _ex bind made for it is removed; the default
# bind's link-dir E stays (it is used again).
def test_a_bind_failure_is_not_tried_again(tmp: TmpDirs) -> None:
    env = env_at(tmp.new(), proc=NO_PROC)
    p = long_path(tmp.new())
    with open(p, "wb"):
        pass
    with raises_errno(standard_errno("bind-exists")):
        _calls.bind(p, env=env, remember=True, trail=(trail := Trail()))
    assert trail.trying == "link-dir"
    assert [s.name for s in trail.skipped] == ["standard"]
    assert entries(env) == []
    with raises_errno(standard_errno("bind-exists")):
        _calls.bind(p, env=env, trail=Trail())
    assert len(entries(env)) == 1


# The default bind leaves the E of its spelling in D (no remove / cleanup to
# call): the spelling keeps naming the path after the listener is closed,
# until the process ends and the system cleans up.
def test_the_default_bind_leaves_its_e(tmp: TmpDirs) -> None:
    env = env_at(tmp.new(), proc=NO_PROC)
    done = bind_in(env, long_path(tmp.new()))
    done.sock.close()
    assert len(entries(env)) == 1


# A spelling by thread-cwd / at holds E, already removed, and a file name: in
# a process whose cwd holds an unrelated file of that name (and anything at
# all), looking it up or unlinking by it reaches nothing.
@pytest.mark.skipif(not HAS_THREAD_ROUTE, reason="no thread cwd / bindat here")
def test_a_dangling_spelling_names_no_unrelated_file(tmp: TmpDirs) -> None:
    cwd = tmp.new()
    with open(cwd + b"/s.sock", "wb") as f:
        f.write(b"unrelated")
    out = run_child(
        """
        from _support import env_at, bind_in, long_path, deep_dir
        from sockat._place import Env, LinkFile
        env = env_at(deep_dir(os.fsencode(ARGS["place"]), 90), proc=b"/nonexistent", link_file=LinkFile.NOT_TRIED)
        done = bind_in(env, long_path(os.fsencode(ARGS["work"])))
        spelling = socket.socket.getsockname(done.sock)
        try:
            os.unlink(spelling)
            hit = True
        except OSError:
            hit = False
        report(route=done.route, spelling=spelling, hit=hit)
        """,
        cwd=cwd,
        args={"place": os.fsdecode(tmp.new()), "work": os.fsdecode(tmp.new())},
    )
    assert out["route"] == THREAD_ROUTE
    assert out["hit"] is False
    with open(cwd + b"/s.sock", "rb") as f:
        assert f.read() == b"unrelated"


# A procB spelling holds D, named at random: once B's fd is closed and its
# number reused for another directory holding a file of the socket's name,
# the spelling still reaches nothing there.
@pytest.mark.skipif(not HAS_PROC, reason="/proc is Linux's")
def test_a_procb_spelling_names_no_file_after_its_fd_is_reused(tmp: TmpDirs) -> None:
    other = tmp.new()
    with open(other + b"/s.sock", "wb") as f:
        f.write(b"unrelated")
    out = run_child(
        """
        from _support import env_at, bind_in, long_path
        env = env_at(os.fsencode(ARGS["place"]))
        done = bind_in(env, long_path(os.fsencode(ARGS["work"])))
        spelling = socket.socket.getsockname(done.sock)
        b = env.auto_base()
        n = b.fd
        env.state.abandon()
        fd = os.open(ARGS["other"], os.O_RDONLY)
        if fd != n:
            os.dup2(fd, n)
        try:
            os.unlink(spelling)
            hit = True
        except OSError:
            hit = False
        report(route=done.route, base=done.base, hit=hit)
        """,
        args={"place": os.fsdecode(tmp.new()), "work": os.fsdecode(tmp.new()), "other": os.fsdecode(other)},
    )
    assert out["route"] == "link-dir"
    assert out["base"] == "proc"
    assert out["hit"] is False
    with open(other + b"/s.sock", "rb") as f:
        assert f.read() == b"unrelated"


# A spelling through B's real path, once cleaned up, reaches nothing: the
# file at the path stays.
def test_a_cleaned_up_spelling_names_nothing(tmp: TmpDirs) -> None:
    env = env_at(tmp.new(), proc=NO_PROC)
    p = long_path(tmp.new())
    done, file = _calls.bind(p, env=env, remember=True)
    assert file is not None
    with done.sock as listener:
        spelling = os.fsencode(kernel_sockname(listener))
        file.cleanup()
        with pytest.raises(FileNotFoundError):
            os.unlink(spelling)
        assert is_socket(p)


# No route reaches a file name too long for every spelling (100 bytes,
# without /proc or a bind that follows link-file, past thread-cwd's 96):
# ENAMETOOLONG, with every route skipped and no D made.
def test_a_name_no_route_reaches_is_enametoolong(tmp: TmpDirs) -> None:
    env = env_at(tmp.new(), proc=NO_PROC, link_file=LinkFile.NOT_TRIED)
    p = long_path(tmp.new(), name=b"n" * (DIRECT_MAX - 7))
    trail = Trail()
    with raises_errno(errno.ENAMETOOLONG):
        bind_in(env, p, trail=trail)
    # thread-cwd / at only where this system's row has it (SPEC: a route
    # the system has no means for is not in its row, nor in skipped).
    assert [s.name for s in trail.skipped] == [
        "standard",
        "link-dir",
        *([THREAD_ROUTE] if THREAD_ROUTE_IN_BIND_ROW else []),
    ]
    assert ds(base_of(env)) == []
    assert listing(os.path.dirname(p)) == []


# Options.base is B: the spelling goes through it, and the diagnostic base
# says "given" (procB or its real path alike).
def test_options_base_carries_the_spelling(tmp: TmpDirs) -> None:
    given = tmp.new() + b"/b"
    env = env_at(tmp.new(), proc=NO_PROC)
    done = bind_in(env, long_path(tmp.new()), opts=_calls.Opts(base=given))
    with done.sock as listener:
        assert done.route == "link-dir"
        assert done.base == "given"
        assert os.fsencode(kernel_sockname(listener)).startswith(os.path.realpath(given) + b"/")


# Where a bind follows link-file's symlink, a bind that fails through it
# (a file at the path: the standard's errno for it) raises from link-file and leaves no E.
@pytest.mark.skipif(not (IS_APPLE or os.uname().sysname == "NetBSD"), reason="a bind follows the symlink here only")
def test_link_file_failure_leaves_no_e(tmp: TmpDirs) -> None:
    env = env_at(tmp.new())
    p = long_path(tmp.new(), name=b"n" * 100)
    with open(p, "wb"):
        pass
    trail = Trail()
    with raises_errno(standard_errno("bind-exists")):
        bind_in(env, p, trail=trail)
    assert trail.trying == "link-file"
    assert entries(env) == []


# The default bind uses link-dir's E again for the same socket directory: many
# binds there leave one E. The _ex bind makes one per call (its cleanup()
# removes its own).
def test_the_default_bind_uses_its_e_again(tmp: TmpDirs) -> None:
    env = env_at(tmp.new(), proc=NO_PROC)
    d = os.path.dirname(long_path(tmp.new()))
    socks = [bind_in(env, d + b"/s%d.sock" % i).sock for i in range(3)]
    try:
        assert len(entries(env)) == 1
        other = bind_in(env, long_path(tmp.new(), name=b"o.sock")).sock
        socks.append(other)
        assert len(entries(env)) == 2
        done, file = _calls.bind(d + b"/ex.sock", env=env, remember=True)
        socks.append(done.sock)
        assert len(entries(env)) == 3
        assert file is not None
        file.cleanup()
        assert len(entries(env)) == 2
    finally:
        for s in socks:
            s.close()


# A shared E removed (or pointing elsewhere) is let go and made again by the
# next default bind, which goes through; a bind that fails leaves the shared
# E, the one an earlier call made and the one it made itself (another default
# bind may be using it; the next bind does).
def test_the_default_bind_remakes_a_removed_e(tmp: TmpDirs) -> None:
    env = env_at(tmp.new(), proc=NO_PROC)
    d = os.path.dirname(long_path(tmp.new()))
    first = bind_in(env, d + b"/a.sock")
    first.sock.close()
    b = env.auto_base()
    assert b is not None
    dd = b.dir(env.config.uid)
    assert dd is not None
    (e,) = entries(env)
    os.unlink(b.real + b"/" + dd.name + b"/" + e)
    second = bind_in(env, d + b"/b.sock")
    second.sock.close()
    (e2,) = entries(env)
    os.unlink(b.real + b"/" + dd.name + b"/" + e2)
    os.symlink(b"/", b.real + b"/" + dd.name + b"/" + e2)
    third = bind_in(env, d + b"/c.sock")
    third.sock.close()
    assert is_socket(d + b"/c.sock")
    kept = entries(env)
    with open(d + b"/taken.sock", "wb"):
        pass
    with raises_errno(standard_errno("bind-exists")):
        bind_in(env, d + b"/taken.sock")
    assert entries(env) == kept
    # A failed default bind into a directory with no E yet: the E it made
    # stays, and the next bind there uses it.
    other = os.path.dirname(long_path(tmp.new()))
    with open(other + b"/taken.sock", "wb"):
        pass
    with raises_errno(standard_errno("bind-exists")):
        bind_in(env, other + b"/taken.sock")
    made = [e for e in entries(env) if e not in kept]
    assert len(made) == 1
    ok = bind_in(env, other + b"/ok.sock")
    ok.sock.close()
    assert [e for e in entries(env) if e not in kept] == made


# Many threads making default binds in one socket directory at once share
# one E, and every bind goes through.
def test_threads_share_the_default_binds_e(tmp: TmpDirs) -> None:
    from concurrent.futures import ThreadPoolExecutor

    env = env_at(tmp.new(), proc=NO_PROC)
    d = os.path.dirname(long_path(tmp.new()))

    def one(i: int) -> str:
        done = bind_in(env, d + b"/t%d.sock" % i)
        done.sock.close()
        return done.route

    with ThreadPoolExecutor(8) as pool:
        routes = list(pool.map(one, range(24)))
    assert set(routes) == {"link-dir"}
    assert len(entries(env)) == 1
    assert all(is_socket(d + b"/t%d.sock" % i) for i in range(24))


# The thread of thread-cwd / thread-cwd-direct works on fds of its own (a dup
# of the directory's fd, handed to it, closed by it): the caller's fd is
# never the one the thread moves to. An interruption in start() is waited
# out: the thread runs to its end before the interruption is raised.
@pytest.mark.skipif(not THREAD_CWD, reason="no thread cwd here")
def test_the_throwaway_thread_owns_its_fds(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    from sockat import _routes

    d = os.path.realpath(tmp.new())
    dirfd = os.open(d, os.O_RDONLY)
    seen: list[tuple[int, str]] = []
    try:
        _routes.in_thread_cwd(dirfd, lambda own: seen.append((own, os.getcwd())))
        ((own, cwd),) = seen
        assert own != dirfd
        assert os.fsencode(cwd) == d
        # The thread closed its own fd (the number is free again or reused,
        # never the caller's).
        assert own != dirfd
        real_start = threading.Thread.start

        def interrupted_start(self: threading.Thread) -> None:
            real_start(self)
            raise KeyboardInterrupt

        monkeypatch.setattr(threading.Thread, "start", interrupted_start)
        ran: list[str] = []
        with pytest.raises(KeyboardInterrupt):
            _routes.in_thread_cwd(dirfd, lambda own: ran.append(os.getcwd()))
        assert [os.fsencode(c) for c in ran] == [d]
    finally:
        os.close(dirfd)


# E's names are drawn from a counter from 0, and the spelling is judged with
# the name E would get before E is made: where a 2-character name no longer
# fits, no E is made (none left in D, shared or not).
def test_e_is_not_made_where_its_name_would_not_fit(tmp: TmpDirs) -> None:
    env = env_at(tmp.new(), proc=NO_PROC, thread_cwd=False, link_file=LinkFile.NOT_TRIED)
    bind_in(env, long_path(tmp.new())).sock.close()
    assert entries(env) == [b"0"]
    b = env.auto_base()
    assert b is not None
    d = b.dir(env.config.uid)
    assert d is not None
    d._next = 36  # the next name is "10"
    name_len = DIRECT_MAX - (len(b.real) + 1 + 10 + 1 + 2 + 1)
    p = long_path(tmp.new(), name=b"n" * (name_len + 1), total=name_len + 60)
    trail = Trail()
    # thread-cwd is off; FreeBSD's at (no thread) may still carry it.
    assert _bind_outcome(env, p, trail) in (None, errno.ENAMETOOLONG)
    assert ("link-dir", "spelling too long") in [(s.name, s.reason) for s in trail.skipped]
    assert entries(env) == [b"0"]


# A B found taken over on the way, then a spelling too long through the next
# place's B: the reason says both (SPEC: the reason tells an occupied B).
def test_the_reason_keeps_an_occupied_b_when_the_next_is_too_long(tmp: TmpDirs) -> None:
    first = tmp.new()
    os.mkdir(first + b"/sockat-%d" % os.geteuid())
    os.chmod(first + b"/sockat-%d" % os.geteuid(), 0o777)
    deep = deep_dir(tmp.new(), 70)
    env = env_at(first, deep, proc=NO_PROC, thread_cwd=False, link_file=LinkFile.NOT_TRIED)
    trail = Trail()
    assert _bind_outcome(env, long_path(tmp.new(), name=b"n" * 20), trail) in (None, errno.ENAMETOOLONG)
    (why,) = [s.reason for s in trail.skipped if s.name == "link-dir"]
    assert "spelling too long" in why
    assert "occupied" in why


# On a system not measured, a failure after link-file's bind went through
# (remembering the file) is the outcome, raised from link-file, not taken for
# a kernel that did not follow the symlink. Forced where a bind follows it.
@pytest.mark.skipif(not (IS_APPLE or os.uname().sysname == "NetBSD"), reason="a bind follows the symlink here only")
def test_link_file_told_at_run_time_keeps_failures_after_the_bind(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sockat._socket_file import Recorder

    env = env_at(tmp.new(), link_file=LinkFile.TOLD_AT_RUN_TIME)
    p = long_path(tmp.new(), name=b"n" * 100)

    def refuse(self: Recorder) -> None:
        raise OSError(errno.ENOTSOCK, "refused for the test")

    monkeypatch.setattr(Recorder, "bound", refuse)
    trail = Trail()
    with raises_errno(errno.ENOTSOCK):
        _calls.bind(p, env=env, remember=True, trail=trail)
    assert trail.trying == "link-file"
    assert entries(env) == []


def _bind_outcome(env: Env, p: bytes, trail: Trail) -> int | None:
    """Bind ``p`` (closing the listener) and give the errno it failed with,
    or ``None``."""
    try:
        bind_in(env, p, trail=trail).sock.close()
    except OSError as e:
        return e.errno
    return None
