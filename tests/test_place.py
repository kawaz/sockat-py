"""B, D and E (hub SPEC-routes "B の作り方", "実体の置き場", "D と E",
SPEC "sweep"): where B is made and what is checked before it is used, D held
by a lock for the life of the process, the processes and threads that make
them at once, and ``sweep``."""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import fcntl
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from _support import (
    HAS_PROC,
    HAS_THREAD_ROUTE,
    NAME_MAX_THREAD_CWD,
    NO_PLACE,
    NO_PROC,
    PRELUDE,
    ROOT,
    TmpDirs,
    accept_ping,
    base_of,
    bind_in,
    connect_in,
    deep_dir,
    ds,
    env_at,
    is_socket,
    listener_at,
    listing,
    long_path,
    new_state,
    raises_errno,
    run_child,
)

import sockat.ex
from sockat import _calls, _place
from sockat._place import Kind
from sockat._routes import Call
from sockat._spelling import Trail
from sockat._sys import DIRECT_MAX, IS_FREEBSD, Op


def uid_name() -> bytes:
    return b"sockat-%d" % os.geteuid()


def mode(p: bytes) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


# B is made in the first place where it can be (sockat-<uid>), 0711, with D
# in it, also 0711 (another uid can follow a spelling through them), named at
# random, whatever the umask.
def test_b_and_d_are_made_in_the_first_place_with_mode_0711(tmp: TmpDirs) -> None:
    first, second = tmp.new(), tmp.new()
    env = env_at(first, second)
    old = os.umask(0o077)
    try:
        done = bind_in(env, long_path(tmp.new()))
    finally:
        os.umask(old)
    done.sock.close()
    b = os.path.realpath(first) + b"/" + uid_name()
    assert listing(first) == [uid_name()]
    assert listing(second) == []
    assert mode(b) == 0o711
    (d,) = ds(b)
    assert len(d) == 10
    assert mode(b + b"/" + d) == 0o711


# A place where B cannot be made (missing) is passed over for the next one;
# the process keeps that B for later calls.
def test_a_place_where_b_cannot_be_made_is_passed_over(tmp: TmpDirs) -> None:
    second = tmp.new()
    env = env_at(b"/nonexistent/place", second)
    for _ in range(2):
        bind_in(env, long_path(tmp.new())).sock.close()
    assert listing(second) == [uid_name()]
    assert len(ds(second + b"/" + uid_name())) == 1


# Every directory from the root to the place must be owned by root or us and
# writable by nobody else, or sticky (an ancestor others can write to lets
# them swap the parent): a place writable by others is not used; a sticky one
# is.
@pytest.mark.parametrize(("place_mode", "used"), [(0o777, False), (0o1777, True), (0o755, True)])
def test_a_place_others_can_write_to_is_used_only_when_sticky(tmp: TmpDirs, place_mode: int, used: bool) -> None:
    first = tmp.new() + b"/p"
    os.mkdir(first)
    os.chmod(first, place_mode)
    second = tmp.new()
    env = env_at(first, second)
    bind_in(env, long_path(tmp.new())).sock.close()
    assert (listing(first) == [uid_name()]) is used
    assert (listing(second) == [uid_name()]) is not used


# An ancestor (not only the place itself) others can write to is refused too.
def test_an_ancestor_others_can_write_to_is_refused(tmp: TmpDirs) -> None:
    top = tmp.new()
    os.chmod(top, 0o777)
    first = top + b"/p"
    os.mkdir(first)
    second = tmp.new()
    bind_in(env_at(first, second), long_path(tmp.new())).sock.close()
    assert listing(first) == []
    assert listing(second) == [uid_name()]


# A B already there is used when it passes the same checks. One that does
# not (others can write to it, a symlink or a file in its place) is taken over
# by someone else (occupied): the place is passed over, nothing is made
# there, and the next place is used; with no next place, the routes using B
# say the B is occupied (SPEC "B の確認").
@pytest.mark.parametrize("what", ["ours", "writable", "symlink", "file"])
def test_an_existing_b_is_checked(tmp: TmpDirs, what: str) -> None:
    place = tmp.new()
    second = tmp.new()
    elsewhere = tmp.new()
    b = place + b"/" + uid_name()
    if what == "ours":
        os.mkdir(b, 0o711)
    elif what == "writable":
        os.mkdir(b)
        os.chmod(b, 0o777)
    elif what == "symlink":
        os.symlink(elsewhere, b)
    else:
        with open(b, "wb"):
            pass
    before = listing(place)
    if what == "ours":
        assert base_of(env_at(place, second)) == os.path.realpath(b)
        return
    assert base_of(env_at(place, second)) == os.path.realpath(second) + b"/" + uid_name()
    assert listing(place) == before
    assert listing(elsewhere) == []
    if what == "writable":
        assert mode(b) == 0o777
    env = env_at(place, proc=NO_PROC, thread_cwd=False, link_file=_place.LinkFile.NOT_TRIED)
    trail = Trail()
    if IS_FREEBSD:
        # FreeBSD's row has at, which needs no thread: with no place for B it
        # goes through D in a B in the socket's directory.
        done = bind_in(env, long_path(tmp.new()), trail=trail)
        done.sock.close()
        assert done.route == "at"
    else:
        with raises_errno(errno.ENAMETOOLONG):
            bind_in(env, long_path(tmp.new()), trail=trail)
    (why,) = [s.reason for s in trail.skipped if s.name == "link-dir"]
    assert "occupied" in why
    assert os.fsdecode(os.path.realpath(place)) in why
    assert listing(place) == before


# Options.base is B itself: made there when missing (its parent must exist),
# used when there; D goes in it.
def test_options_base_is_b(tmp: TmpDirs) -> None:
    root = tmp.new()
    given = root + b"/b"
    bound = sockat.ex.bind(long_path(tmp.new()), options=sockat.ex.Options(base=given))
    with bound.socket:
        assert mode(given) == 0o711
        assert len(ds(given)) == 1
        # A short B: link-dir, by procB where /proc is and by its real path
        # elsewhere; base is "given" either way (SPEC "診断": given is
        # Options.base's B, procB or real path alike), so the route and base
        # are the same on every system.
        assert bound.route == "link-dir"
        assert bound.base == "given"
        bound.file.remove()


# Options.base that cannot be B is an error, and no automatic place is used
# instead: a missing parent ENOENT, a parent or B that is not a directory
# ENOTDIR, B or an ancestor others can write to EPERM; with usable_cross_uid,
# a way to it others cannot search EPERM.
@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("no parent", errno.ENOENT),
        ("parent is a file", errno.ENOTDIR),
        ("b is a file", errno.ENOTDIR),
        ("b writable by others", errno.EPERM),
        ("ancestor writable by others", errno.EPERM),
        ("b is a symlink", errno.EPERM),
        ("cross uid through 0700", errno.EPERM),
    ],
)
def test_options_base_that_cannot_be_b_is_an_error(tmp: TmpDirs, case: str, code: int) -> None:
    root = tmp.new()
    auto = tmp.new()
    given = root + b"/b"
    cross = False
    if case == "no parent":
        given = root + b"/none/b"
    elif case == "parent is a file":
        with open(root + b"/f", "wb"):
            pass
        given = root + b"/f/b"
    elif case == "b is a file":
        with open(given, "wb"):
            pass
    elif case == "b writable by others":
        os.mkdir(given)
        os.chmod(given, 0o777)
    elif case == "ancestor writable by others":
        os.chmod(root, 0o777)
    elif case == "b is a symlink":
        os.symlink(auto, given)
    else:
        os.chmod(root, 0o700)
        cross = True
    before = sorted(os.listdir(root))
    env = env_at(auto)
    with raises_errno(code):
        _calls.bind(long_path(tmp.new()), env=env, opts=_calls.Opts(usable_cross_uid=cross, base=given), remember=True)
    assert listing(auto) == []
    if case not in ("cross uid through 0700",):
        assert sorted(os.listdir(root)) == before


# usable_cross_uid takes B only from a place others can search their way to,
# never the /proc spelling, and says so in the Spelling. A place under a 0700
# directory is passed over for one that is not.
def test_usable_cross_uid_takes_b_others_can_reach(tmp: TmpDirs) -> None:
    closed = tmp.new()  # 0700 (mkdtemp)
    open_ = tmp.new()
    os.chmod(open_, 0o755)
    env = env_at(closed, open_, proc=NO_PROC)
    done = bind_in(env, long_path(tmp.new()), opts=_calls.Opts(usable_cross_uid=True))
    with done.sock:
        assert done.route == "link-dir"
        assert done.base == "runtime"
        assert isinstance(done.spelling, sockat.ex.Linked)
        assert done.spelling.usable_cross_uid
        assert listing(open_) == [uid_name()]
        assert listing(closed) == []


# Where no place lets others through, usable_cross_uid does not fall back to a
# spelling they cannot follow: link-dir and link-file are skipped, and the
# bind goes on to thread-cwd / at (Dangling) or ENAMETOOLONG.
def test_usable_cross_uid_without_such_a_place_does_not_fall_back(tmp: TmpDirs) -> None:
    closed = tmp.new()
    env = env_at(closed)
    trail = Trail()
    opts = _calls.Opts(usable_cross_uid=True)
    if not HAS_THREAD_ROUTE:
        with raises_errno(errno.ENAMETOOLONG):
            bind_in(env, long_path(tmp.new()), opts=opts, trail=trail)
        assert [s.name for s in trail.skipped][:2] == ["standard", "link-dir"]
        return
    done = bind_in(env, long_path(tmp.new()), opts=opts, trail=trail)
    with done.sock:
        assert done.spelling == sockat.ex.Dangling()
        assert done.route in ("thread-cwd", "at")


# D carries a shared lock for as long as the process holds it: an exclusive
# lock (what sweep takes) cannot be had on it from another open file.
def test_d_is_held_with_a_shared_lock(tmp: TmpDirs) -> None:
    env = env_at(tmp.new())
    bind_in(env, long_path(tmp.new())).sock.close()
    b = base_of(env)
    (d,) = ds(b)
    fd = os.open(b + b"/" + d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    finally:
        os.close(fd)


# A sweep of another process can take a D between its mkdirat and the lock
# (no lock on it yet) and remove it: the maker looks the name up again once
# locked, finds it gone, and makes another.
def test_d_removed_before_the_lock_is_made_again(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    env = env_at(tmp.new())
    b = env.auto_base()
    assert b is not None
    real_flock = fcntl.flock
    removed: list[bytes] = []

    def flock(fd: int, op: int) -> None:
        if not removed:
            # As a sweep would: the D just made, not locked yet, is removed.
            name = ds(b.real)[0]
            os.rmdir(b.real + b"/" + name)
            removed.append(name)
        real_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", flock)
    d = b.dir(env.config.uid)
    assert d is not None
    assert removed
    assert d.name != removed[0]
    assert ds(b.real) == [d.name]


# Many threads binding and connecting at once share the process's one B and
# one D, and every call goes through.
def test_threads_at_once_share_one_b_and_one_d(tmp: TmpDirs) -> None:
    place = tmp.new()
    env = env_at(place)
    paths = [long_path(tmp.new(), name=b"s%d.sock" % i) for i in range(24)]

    def one(p: bytes) -> None:
        done = bind_in(env, p)
        with done.sock as listener:
            client, _ = connect_in(env, p)
            with client:
                accept_ping(listener, client)
        assert is_socket(p)

    with ThreadPoolExecutor(12) as pool:
        list(pool.map(one, paths))
    b = base_of(env)
    assert listing(place) == [uid_name()]
    assert len(ds(b)) == 1


# Processes binding at once in the same place share B and each makes a D of
# its own; none fails on another's B or D.
def test_processes_at_once_share_b_and_make_a_d_each(tmp: TmpDirs) -> None:
    place = tmp.new()
    work = tmp.new()
    code = PRELUDE + textwrap.dedent(
        f"""
        from _support import env_at, bind_in, long_path, connect_in, accept_ping
        env = env_at({place!r})
        sys.stdin.readline()
        for i in range(5):
            p = long_path({work!r} + b"/%d-%d" % (os.getpid(), i))
            done = bind_in(env, p)
            c, _ = connect_in(env, p)
            accept_ping(done.sock, c)
        print("ok", flush=True)
        sys.stdin.readline()
        """
    )
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for _ in range(6)
    ]
    try:
        for c in children:
            assert c.stdin is not None
            c.stdin.write(b"go\n")
            c.stdin.flush()
        for c in children:
            assert c.stdout is not None
            line = c.stdout.readline()
            assert line == b"ok\n", c.stderr.read() if c.stderr else line
        b = os.path.realpath(place) + b"/" + uid_name()
        assert listing(place) == [uid_name()]
        assert len(ds(b)) == 6
    finally:
        for c in children:
            assert c.stdin is not None
            c.stdin.close()
            c.wait()


# sweep removes the D of a process that has ended (its lock gone), with the E
# in it, and leaves the D of a live process (another one, and this one) alone.
def test_sweep_removes_only_the_d_of_ended_processes(tmp: TmpDirs) -> None:
    place = tmp.new()
    work = tmp.new()
    code = PRELUDE + textwrap.dedent(
        f"""
        from _support import env_at, bind_in, long_path
        env = env_at({place!r})
        bind_in(env, long_path({work!r} + b"/%d" % os.getpid()))
        print("ok", flush=True)
        sys.stdin.readline()
        """
    )
    live = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    ended = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for c in (live, ended):
            assert c.stdout is not None
            assert c.stdout.readline() == b"ok\n"
        assert ended.stdin is not None
        ended.stdin.close()
        ended.wait()
        env = env_at(place)
        bind_in(env, long_path(work + b"/self")).sock.close()
        b = base_of(env)
        assert len(ds(b)) == 3
        mine = env.auto_base()
        assert mine is not None
        mine_d = mine.dir(env.config.uid)
        assert mine_d is not None
        _place.sweep(env)
        left = ds(b)
        assert len(left) == 2
        assert mine_d.name in left
    finally:
        assert live.stdin is not None
        live.stdin.close()
        live.wait()
    _place.sweep(env)
    assert ds(b) == [mine_d.name]


# sweep makes no B where there is none, and touches nothing but the D in B.
def test_sweep_makes_nothing_and_leaves_other_things_in_b(tmp: TmpDirs) -> None:
    empty = tmp.new()
    place = tmp.new()
    b = place + b"/" + uid_name()
    os.mkdir(b, 0o711)
    os.symlink(b"/nowhere", b + b"/E1")
    with open(b + b"/file", "wb"):
        pass
    _place.sweep(env_at(empty, place))
    assert listing(empty) == []
    assert listing(b) == [b"E1", b"file"]


# A child made by fork does not use its parent's B and D (their fds are the
# parent's): it makes its own D, and the parent's D stays as it was.
def test_a_forked_child_makes_its_own_d(tmp: TmpDirs) -> None:
    place = tmp.new()
    work = tmp.new()
    out = run_child(
        f"""
        from _support import long_path
        from sockat import _place, _calls
        from sockat._place import Env, Config, Place, Kind
        def env():
            return Env(Config(places=(Place(Kind.TMP, {place!r}, b"sockat-%d" % os.geteuid()),)), _place.state())
        _calls.bind(long_path({work!r} + b"/parent"), env=env())
        parent_d = _place.state().auto.dir(os.geteuid()).name
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            _calls.bind(long_path({work!r} + b"/child"), env=env())
            os.write(w, _place.state().auto.dir(os.geteuid()).name)
            os._exit(0)
        os.waitpid(pid, 0)
        still = _place.state().auto.dir(os.geteuid()).name.decode()
        report(parent=parent_d.decode(), child=os.read(r, 100).decode(), still=still)
        """
    )
    assert out["parent"] != out["child"]
    assert out["still"] == out["parent"]


# The last place is the socket's directory: B there is named
# .sockat.XXXXXXXXXX, and the process keeps one per directory.
def test_b_in_the_socket_directory_is_the_last_place(tmp: TmpDirs) -> None:
    env = _place.Env(_place.Config(places=()), new_state())
    sockdir = os.path.dirname(long_path(tmp.new()))
    real = os.path.realpath(sockdir)
    b = env.sockdir_base(real)
    assert b is not None
    assert b.kind is Kind.SOCKDIR
    assert os.path.basename(b.real).startswith(b".sockat.")
    assert env.sockdir_base(real) is b


# A place this process cannot write to (B cannot be made) is passed over.
@pytest.mark.skipif(ROOT, reason="root writes anywhere")
def test_a_read_only_place_is_passed_over(tmp: TmpDirs) -> None:
    first = tmp.new()
    os.chmod(first, 0o500)
    second = tmp.new()
    env = env_at(first, second)
    assert os.path.dirname(base_of(env)) == os.path.realpath(second)


# Threads asking for the process's B at once get one and the same B.
def test_threads_making_b_at_once(tmp: TmpDirs) -> None:
    place = tmp.new()
    env = env_at(place)
    got: list[bytes] = []
    lock = threading.Lock()

    def one() -> None:
        r = base_of(env)
        with lock:
            got.append(r)

    ts = [threading.Thread(target=one) for _ in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(set(got)) == 1


# Options.base is checked once the socket's directory is looked up and before
# any route, for a bind and a connect alike, making nothing: a failure has no
# route and no skipped, and nothing appears (even where an earlier connect
# route would have reached the socket without B).
@pytest.mark.parametrize("op", ["bind", "connect"])
def test_options_base_is_checked_before_any_route(tmp: TmpDirs, op: str) -> None:
    root = tmp.new()
    os.chmod(root, 0o777)
    given = root + b"/b"
    p = long_path(tmp.new())
    with listener_at(p) if op == "connect" else contextlib.nullcontext(), raises_errno(errno.EPERM) as e:
        if op == "bind":
            sockat.ex.bind(p, options=sockat.ex.Options(base=given))
        else:
            sockat.ex.connect(p, options=sockat.ex.Options(base=given))
    assert e.value.route is None  # type: ignore[attr-defined]
    assert e.value.skipped == ()  # type: ignore[attr-defined]
    assert listing(root) == []


# Options.base that is not an absolute path ending with a name is an argument
# error (ValueError), whatever the path to bind / connect.
@pytest.mark.parametrize("given", [b"relative/b", b"/tmp/x/", b"/tmp/x/..", b"/"])
def test_options_base_must_be_an_absolute_path_ending_with_a_name(tmp: TmpDirs, given: bytes) -> None:
    p = long_path(tmp.new())
    for call in (sockat.ex.bind, sockat.ex.connect, sockat.ex.resolve_for_bind, sockat.ex.resolve_for_connect):
        with pytest.raises(ValueError, match=r"base|file name"):
            call(p, options=sockat.ex.Options(base=given))
    with pytest.raises(ValueError, match=r"base|file name"):
        sockat.ex.sweep_base(given)


# A connect's spelling stays nowhere: usable_cross_uid is not looked at by
# the connect calls (a base others cannot reach is not refused for it).
def test_connects_do_not_look_at_usable_cross_uid(tmp: TmpDirs) -> None:
    root = tmp.new()  # 0700: others cannot search it
    p = long_path(tmp.new())
    o = sockat.ex.Options(usable_cross_uid=True, base=root + b"/b")
    with listener_at(p) as listener:
        c = sockat.ex.connect(p, options=o)
        with c.socket:
            accept_ping(listener, c.socket)
        r = sockat.ex.resolve_for_connect(p, options=o)
        r.cleanup()
    with raises_errno(errno.EPERM):
        sockat.ex.resolve_for_bind(p, options=o)


# B is not made where the spelling through it is known not to fit (judged
# from the places' real paths), for a bind and the default connect.
def test_b_is_not_made_for_a_spelling_that_cannot_fit(tmp: TmpDirs) -> None:
    place = deep_dir(tmp.new(), 95)
    env = env_at(place, proc=NO_PROC, thread_cwd=False, link_file=_place.LinkFile.NOT_TRIED)
    # A file name past thread-cwd / at too (FreeBSD's at needs no thread and
    # would make B for its D), so that no route of any system's row takes B.
    p = long_path(tmp.new(), name=b"n" * (NAME_MAX_THREAD_CWD + 1))
    with raises_errno(errno.ENAMETOOLONG):
        bind_in(env, p)
    with listener_at(p), raises_errno(errno.ENAMETOOLONG):
        connect_in(env, p, order=("link-file",))
    assert listing(place) == []


# Whatever the umask (even one that takes the owner's r and x off), B and D
# are made 0711: their mode is set by name right after the mkdirat, before
# they are opened, and a bind through them goes through.
@pytest.mark.parametrize("umask", [0o077, 0o700, 0o777])
def test_b_and_d_are_0711_whatever_the_umask(tmp: TmpDirs, umask: int) -> None:
    place = tmp.new()
    out = run_child(
        """
        from _support import env_at, bind_in
        os.umask(ARGS["umask"])
        env = env_at(os.fsencode(ARGS["place"]), proc=b"/nonexistent")
        done = bind_in(env, os.fsencode(ARGS["path"]))
        report(route=done.route)
        """,
        args={"place": os.fsdecode(place), "path": os.fsdecode(long_path(tmp.new())), "umask": umask},
    )
    assert out["route"] == "link-dir"
    b = place + b"/" + uid_name()
    (d,) = ds(b)
    assert mode(b) == 0o711
    assert mode(b + b"/" + d) == 0o711


# A B already there that is ours but 0700 is used as it is (its mode is not
# changed); others cannot follow spellings through it, so usable_cross_uid
# does not take it (and goes on to thread-cwd / at, or ENAMETOOLONG without
# them).
def test_an_existing_0700_b_is_used_as_it_is(tmp: TmpDirs) -> None:
    place = tmp.new()
    os.chmod(place, 0o755)
    b = place + b"/" + uid_name()
    os.mkdir(b, 0o700)
    os.chmod(b, 0o700)
    env = env_at(place, proc=NO_PROC)
    done = bind_in(env, long_path(tmp.new()))
    done.sock.close()
    assert base_of(env) == os.path.realpath(b)
    assert mode(b) == 0o700
    assert isinstance(done.spelling, sockat.ex.Linked)
    assert not done.spelling.usable_cross_uid
    opts = _calls.Opts(usable_cross_uid=True)
    if not HAS_THREAD_ROUTE:
        with raises_errno(errno.ENAMETOOLONG):
            bind_in(env, long_path(tmp.new()), opts=opts)
        return
    cross = bind_in(env, long_path(tmp.new()), opts=opts)
    cross.sock.close()
    assert cross.spelling == sockat.ex.Dangling()


# usable_with_private_tmp: a spelling through B in a place under /tmp (or
# whose real path is) cannot be followed with a private /tmp; one in a place
# elsewhere can.
def test_usable_with_private_tmp_follows_the_place(tmp: TmpDirs) -> None:
    under_tmp = tmp.new()
    elsewhere = _not_under_tmp(tmp)
    for place, want in ((under_tmp, False), (elsewhere, True)):
        env = env_at(place, proc=NO_PROC)
        done = bind_in(env, long_path(tmp.new()))
        done.sock.close()
        assert isinstance(done.spelling, sockat.ex.Linked)
        assert done.spelling.usable_with_private_tmp is want, place


def _not_under_tmp(tmp: TmpDirs) -> bytes:
    """A new directory under neither /tmp nor /var/tmp (their realpaths
    included): ``$TMPDIR`` when it is so (macOS), else ``/dev/shm`` (Linux)."""
    for candidate in (os.environ.get("TMPDIR"), "/dev/shm"):
        if not candidate or not os.path.isdir(candidate) or not os.access(candidate, os.W_OK):
            continue
        if _place.goes_through_shared_tmp(os.fsencode(os.path.realpath(candidate))):
            continue
        d = tmp.new(under=candidate)
        if _place.goes_through_shared_tmp(os.fsencode(os.path.realpath(d))):
            continue
        return d
    pytest.skip("no writable place outside /tmp here")


# sweep leaves a D holding anything but symlinks (not one sockat made as it
# is), never touches the E right in B, and looks only at directories named
# as D is (10 letters and digits): B is sockat's alone, but anything else in
# it is left.
def test_sweep_leaves_a_d_with_more_than_symlinks(tmp: TmpDirs) -> None:
    place = tmp.new()
    b = place + b"/" + uid_name()
    os.mkdir(b, 0o711)
    os.mkdir(b + b"/Dead567890", 0o711)
    os.symlink(b"/nowhere", b + b"/Dead567890/1")
    os.mkdir(b + b"/Kept567890", 0o711)
    with open(b + b"/Kept567890/file", "wb"):
        pass
    for other in (b"short", b"Long5678901", b"dash-67890"):
        os.mkdir(b + b"/" + other, 0o711)
    os.symlink(b"/nowhere", b + b"/Elink67890")
    _place.sweep(env_at(place))
    assert listing(b) == sorted([b"Kept567890", b"short", b"Long5678901", b"dash-67890", b"Elink67890"])


# sweep_base does for Options.base's B what sweep does for the automatic
# places: the D of a process gone is removed, a live one stays.
def test_sweep_base(tmp: TmpDirs) -> None:
    given = tmp.new() + b"/b"
    work = tmp.new()
    out = run_child(
        """
        import sockat.ex
        from _support import long_path
        o = sockat.ex.Options(base=os.fsencode(ARGS["given"]))
        b = sockat.ex.bind(long_path(os.fsencode(ARGS["work"])), options=o)
        report(route=b.route)
        """,
        args={"given": os.fsdecode(given), "work": os.fsdecode(work)},
    )
    assert out["route"] == "link-dir"
    mine = sockat.ex.bind(long_path(work + b"/mine"), options=sockat.ex.Options(base=given))
    with mine.socket:
        assert len(ds(given)) == 2
        sockat.ex.sweep_base(given)
        assert len(ds(given)) == 1
        mine.file.remove()


# What the process makes is owned by its effective uid, and B is named after
# it: a process whose effective uid differs from its real one (seteuid) makes
# sockat-<euid>, owned by it, and uses it. Run as root only (seteuid).
@pytest.mark.skipif(not ROOT, reason="needs root to seteuid")
def test_b_is_made_for_the_effective_uid(tmp: TmpDirs) -> None:
    place = tmp.new()
    os.chmod(place, 0o1777)
    work = tmp.new()
    os.chmod(work, 0o1777)
    out = run_child(
        """
        from _support import env_at, bind_in, long_path
        os.seteuid(65534)
        env = env_at(os.fsencode(ARGS["place"]), proc=b"/nonexistent")
        done = bind_in(env, long_path(os.fsencode(ARGS["work"])))
        report(route=done.route, base=done.base)
        """,
        args={"place": os.fsdecode(place), "work": os.fsdecode(work)},
    )
    assert out["route"] == "link-dir"
    assert listing(place) == [b"sockat-65534"]
    assert os.stat(place + b"/sockat-65534").st_uid == 65534


# A fresh B that fails its check (owned by another uid than the one it is
# made for) is removed rather than left in the place.
@pytest.mark.skipif(not ROOT, reason="needs root to make a directory another uid owns")
def test_a_fresh_b_failing_its_check_is_removed(tmp: TmpDirs) -> None:
    place = tmp.new()
    env = env_at(place, proc=NO_PROC)
    env = _place.Env(dataclasses.replace(env.config, uid=65534), new_state())
    assert env.auto_base() is None
    assert listing(place) == []


# The process's B and the B for usable_cross_uid are one and the same where
# they are at the same place: one B, one D.
def test_cross_uid_and_plain_share_b_and_d_at_one_place(tmp: TmpDirs) -> None:
    place = tmp.new()
    os.chmod(place, 0o755)
    env = env_at(place, proc=NO_PROC)
    for cross in (False, True, False):
        done = bind_in(env, long_path(tmp.new()), opts=_calls.Opts(usable_cross_uid=cross))
        done.sock.close()
        assert done.route == "link-dir"
    assert listing(place) == [uid_name()]
    assert len(ds(place + b"/" + uid_name())) == 1


# D that cannot be made in a call leaves that call without the routes using
# D (no other B is looked for), and a later call makes it.
@pytest.mark.skipif(ROOT, reason="root writes anywhere")
def test_d_not_made_is_tried_again_by_a_later_call(tmp: TmpDirs) -> None:
    first, second = tmp.new(), tmp.new()
    env = env_at(first, second, proc=NO_PROC, thread_cwd=False, link_file=_place.LinkFile.NOT_TRIED)
    b = base_of(env)
    os.chmod(b, 0o511)
    try:
        trail = Trail()
        with raises_errno(errno.ENAMETOOLONG):
            bind_in(env, long_path(tmp.new()), trail=trail)
        assert ("link-dir", "D not made") in [(s.name, s.reason) for s in trail.skipped]
        assert listing(second) == []
    finally:
        os.chmod(b, 0o711)
    done = bind_in(env, long_path(tmp.new()))
    done.sock.close()
    assert done.route == "link-dir"
    assert len(ds(b)) == 1


def _fake_proc(root: bytes) -> bytes:
    """A /proc whose fd entries all name ``/``: it lists an fd, but not as
    what the fd is (another PID namespace's /proc, say)."""
    for who in (b"self", b"%d" % os.getpid()):
        fds = root + b"/" + who + b"/fd"
        os.makedirs(fds)
        for n in range(256):
            os.symlink(b"/", fds + b"/%d" % n)
    return root


# procB is used only where /proc names B: when /proc/<pid>/fd/<B's fd> is
# another directory than B (its (st_dev, st_ino) differ), link-dir goes
# through B's real path instead.
def test_procb_is_not_used_where_proc_names_another_directory(tmp: TmpDirs) -> None:
    proc = _fake_proc(tmp.new() + b"/proc")
    env = env_at(tmp.new(), proc=proc)
    trail = Trail()
    done = bind_in(env, long_path(tmp.new()), trail=trail)
    done.sock.close()
    assert done.route == "link-dir"
    assert done.base == "tmp"
    assert isinstance(done.spelling, sockat.ex.Linked)
    assert done.spelling.until is sockat.ex.Until.CLEANUP


# Where no place can hold B, B is made in the socket's directory and used by
# procB only: the bind goes through it (base "sockdir").
@pytest.mark.skipif(not HAS_PROC, reason="procB needs /proc")
def test_a_bind_through_b_in_the_socket_directory(tmp: TmpDirs) -> None:
    env = env_at(NO_PLACE)
    p = long_path(tmp.new())
    done = bind_in(env, p)
    with done.sock:
        assert done.route == "link-dir"
        assert done.base == "sockdir"
        assert isinstance(done.spelling, sockat.ex.Linked)
        assert done.spelling.until is sockat.ex.Until.PROCESS
        (b,) = [n for n in listing(os.path.dirname(p)) if n.startswith(b".sockat.")]
        assert len(ds(os.path.dirname(p) + b"/" + b)) == 1


# usable_cross_uid with no B others can reach goes on to thread-cwd / at, with
# D made in the process's usual B (not one more B).
@pytest.mark.skipif(not HAS_THREAD_ROUTE, reason="no thread cwd / bindat here")
def test_cross_uid_falling_to_thread_cwd_uses_the_usual_b(tmp: TmpDirs) -> None:
    closed = tmp.new()  # 0700
    env = env_at(closed, proc=NO_PROC)
    done = bind_in(env, long_path(tmp.new()), opts=_calls.Opts(usable_cross_uid=True))
    done.sock.close()
    assert done.spelling == sockat.ex.Dangling()
    assert listing(closed) == [uid_name()]
    assert len(ds(closed + b"/" + uid_name())) == 1


# An Options.base already there, ours but 0700, is refused for
# usable_cross_uid (others cannot search it): EPERM, before any route.
def test_an_existing_0700_base_with_usable_cross_uid_is_eperm(tmp: TmpDirs) -> None:
    root = tmp.new()
    os.chmod(root, 0o755)
    given = root + b"/b"
    os.mkdir(given)
    os.chmod(given, 0o700)
    trail = Trail()
    with raises_errno(errno.EPERM):
        _calls.bind(
            long_path(tmp.new()),
            env=env_at(tmp.new()),
            opts=_calls.Opts(usable_cross_uid=True, base=given),
            trail=trail,
        )
    assert trail.skipped == []
    assert listing(given) == []


# Options.base checked but not makeable (its parent read-only): the mkdirat's
# error, with no route and nothing but the routes skipped before.
@pytest.mark.skipif(ROOT, reason="root writes anywhere")
@pytest.mark.parametrize("op", ["bind", "connect"])
def test_options_base_that_cannot_be_made(tmp: TmpDirs, op: str) -> None:
    root = tmp.new()
    os.chmod(root, 0o555)
    given = root + b"/b"
    p = long_path(tmp.new())
    trail = Trail()
    env = env_at(tmp.new(), proc=NO_PROC, thread_cwd=False, wide=False)
    try:
        if op == "bind":
            with pytest.raises(PermissionError):
                _calls.bind(p, env=env, opts=_calls.Opts(base=given), trail=trail)
        else:
            with listener_at(p), pytest.raises(PermissionError):
                connect_in_opts(env, p, given, trail)
    finally:
        os.chmod(root, 0o755)
    assert trail.trying is None
    assert "link-dir" not in [s.name for s in trail.skipped]
    assert "link-file" not in [s.name for s in trail.skipped]


def connect_in_opts(env: _place.Env, p: bytes, given: bytes, trail: Trail) -> None:
    _calls.connect(p, call=Call(Op.CONNECT), env=env, opts=_calls.Opts(base=given), trail=trail, order=("link-file",))


# A B removed while the process holds it (an old systemd-tmpfiles, macOS's
# cleaning of $TMPDIR, by hand) is let go by the next call and made again:
# the bind and the connect after it go through B as before.
def test_b_removed_is_made_again(tmp: TmpDirs) -> None:
    place = tmp.new()
    env = env_at(place, proc=NO_PROC, thread_cwd=False)
    bind_in(env, long_path(tmp.new())).sock.close()
    b = base_of(env)
    shutil.rmtree(b)
    done = bind_in(env, long_path(tmp.new()))
    done.sock.close()
    assert done.route == "link-dir"
    assert base_of(env) == b
    assert len(ds(b)) == 1
    shutil.rmtree(b)
    p = long_path(tmp.new())
    with listener_at(p) as listener:
        sock, route = connect_in(env, p, order=("link-file",))
        with sock:
            accept_ping(listener, sock)
    assert route == "link-file"
    assert listing(place) == [uid_name()]


# A D removed while held is let go and made again by the next bind.
def test_d_removed_is_made_again(tmp: TmpDirs) -> None:
    env = env_at(tmp.new(), proc=NO_PROC, thread_cwd=False)
    bind_in(env, long_path(tmp.new())).sock.close()
    b = base_of(env)
    (old,) = ds(b)
    shutil.rmtree(b + b"/" + old)
    done = bind_in(env, long_path(tmp.new()))
    done.sock.close()
    assert done.route == "link-dir"
    (new,) = ds(b)
    assert new != old


# A route the system has no means for is not in its row, so not in skipped:
# where no thread can be given a cwd of its own (NetBSD, OpenBSD, illumos),
# neither the bind's thread-cwd nor the connect's thread-cwd-direct shows.
# Stood in for here by turning the means off; where the means exist but are
# turned off (or refused), the route is skipped with its reason.
def test_routes_without_means_are_not_skipped(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    from sockat import _routes

    env = env_at(tmp.new(), proc=NO_PROC, thread_cwd=False, link_file=_place.LinkFile.NOT_TRIED)
    name = b"n" * (DIRECT_MAX - 7)
    for has in (True, False):
        monkeypatch.setattr(_routes, "HAS_THREAD_CWD", has)
        monkeypatch.setattr(_routes, "IS_FREEBSD", False)
        trail = Trail()
        with raises_errno(errno.ENAMETOOLONG):
            bind_in(env, long_path(tmp.new(), name=name), trail=trail)
        assert ("thread-cwd" in [s.name for s in trail.skipped]) is has
        assert ("thread-cwd-direct" in _routes.connect_order(env, False)) is has


# D whose mode cannot be set or that cannot be opened is removed (the empty
# directory just made), and that call goes without the routes using D: no
# other B is looked for, nothing is left in B.
@pytest.mark.parametrize("fail", ["chmod", "open"])
def test_d_that_cannot_be_made_is_removed(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, fail: str) -> None:
    first, second = tmp.new(), tmp.new()
    env = env_at(first, second, proc=NO_PROC, thread_cwd=False, link_file=_place.LinkFile.NOT_TRIED)
    b = base_of(env)
    held = env.auto_base()
    assert held is not None

    def in_b(dir_fd: int | None) -> bool:
        return dir_fd == held.fd

    if fail == "chmod":
        real_chmod = os.chmod

        def chmod(path: bytes, mode: int, *, dir_fd: int | None = None, follow_symlinks: bool = True) -> None:
            if in_b(dir_fd):
                raise PermissionError(13, "refused for the test")
            real_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(os, "chmod", chmod)
    else:
        real_open = _place._open_own

        def open_own(name: bytes, dir_fd: int) -> int:
            if in_b(dir_fd):
                raise PermissionError(13, "refused for the test")
            return real_open(name, dir_fd)

        monkeypatch.setattr(_place, "_open_own", open_own)
    trail = Trail()
    with raises_errno(errno.ENAMETOOLONG):
        bind_in(env, long_path(tmp.new()), trail=trail)
    monkeypatch.undo()
    assert ("link-dir", "D not made") in [(s.name, s.reason) for s in trail.skipped]
    assert listing(b) == []
    assert listing(second) == []


# D that could not be made is remembered per B for the rest of the call: the
# routes after the first that wanted it do not try to make it again on the
# same B; the next call does.
def test_d_not_made_is_not_tried_again_within_the_call(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    env = env_at(tmp.new(), proc=NO_PROC, link_file=_place.LinkFile.NOT_TRIED)
    calls: list[bytes] = []

    def no_d(base: _place.Base, uid: int) -> None:
        calls.append(base.real)

    monkeypatch.setattr(_place, "make_dir", no_d)
    for n in (1, 2):
        trail = Trail()
        with raises_errno(errno.ENAMETOOLONG):
            bind_in(env, long_path(tmp.new()), trail=trail)
        # link-dir and thread-cwd (where there is one) both want D: one try.
        assert len(calls) == n


# The B to estimate the spelling's length against is the first place not
# known to be out without making anything (missing, unsafe on the way): when
# the spelling cannot fit through it, no B is made there or in a later
# place, even a shorter one.
def test_the_estimate_is_against_the_first_usable_place(tmp: TmpDirs) -> None:
    unsafe = tmp.new()
    os.chmod(unsafe, 0o777)
    long_place = deep_dir(tmp.new(), 95)
    short_place = tmp.new()
    env = env_at(b"/nonexistent/place", unsafe, long_place, short_place, proc=NO_PROC, thread_cwd=False)
    env = _place.Env(dataclasses.replace(env.config, link_file=_place.LinkFile.NOT_TRIED), env.state)
    trail = Trail()
    if IS_FREEBSD:
        # FreeBSD's row has at, which needs no thread: the bind goes through it,
        # with D in the first place B can be made (the long one), and the
        # shorter place after it is not used.
        done = bind_in(env, long_path(tmp.new()), trail=trail)
        done.sock.close()
        assert done.route == "at"
        assert listing(long_place) == [uid_name()]
    else:
        with raises_errno(errno.ENAMETOOLONG):
            bind_in(env, long_path(tmp.new()), trail=trail)
        assert listing(long_place) == []
    assert ("link-dir", "spelling too long") in [(s.name, s.reason) for s in trail.skipped]
    for place in (unsafe, short_place):
        assert listing(place) == []


# Options.base's safety is checked on every call (an ancestor made writable by
# others since is refused), while whether others can search their way to it
# (usable_cross_uid) is judged when the B is first held and kept.
def test_options_base_safety_every_call_cross_uid_judged_once(tmp: TmpDirs) -> None:
    root = tmp.new()
    os.chmod(root, 0o755)
    given = root + b"/b"
    env = env_at(tmp.new(), proc=NO_PROC)
    opts = _calls.Opts(usable_cross_uid=True, base=given)
    first = bind_in(env, long_path(tmp.new()), opts=opts)
    first.sock.close()
    assert isinstance(first.spelling, sockat.ex.Linked)
    assert first.spelling.usable_cross_uid
    os.chmod(root, 0o711 & ~0o001)  # others can no longer search it
    try:
        again = bind_in(env, long_path(tmp.new()), opts=opts)
        again.sock.close()
        assert isinstance(again.spelling, sockat.ex.Linked)
        assert again.spelling.usable_cross_uid
        os.chmod(root, 0o777)
        with raises_errno(errno.EPERM):
            bind_in(env, long_path(tmp.new()), opts=opts)
    finally:
        os.chmod(root, 0o755)


# sweep_base(): a missing parent or B is nothing to sweep (success); a base
# of the wrong form is an argument error; a symlink is EPERM, a file
# ENOTDIR; a B others (group included) can write to, or one on a way others
# can write to, is EPERM. Nothing is made.
@pytest.mark.parametrize(
    ("case", "outcome"),
    [
        ("no parent", None),
        ("no b", None),
        ("symlink", errno.EPERM),
        ("file", errno.ENOTDIR),
        ("b writable", errno.EPERM),
        ("b group-writable", errno.EPERM),
        ("ancestor writable", errno.EPERM),
        ("ours", None),
    ],
)
def test_sweep_base_outcomes(tmp: TmpDirs, case: str, outcome: int | None) -> None:
    root = tmp.new()
    os.chmod(root, 0o755)
    given = root + b"/b"
    if case == "no parent":
        given = root + b"/none/b"
    elif case == "symlink":
        os.symlink(root, given)
    elif case == "file":
        with open(given, "wb"):
            pass
    elif case in ("b writable", "b group-writable"):
        os.mkdir(given)
        os.chmod(given, 0o777 if case == "b writable" else 0o775)
    elif case == "ancestor writable":
        os.chmod(root, 0o777)
        os.mkdir(given, 0o711)
    elif case == "ours":
        os.mkdir(given, 0o711)
    before = sorted(os.listdir(root))
    if outcome is None:
        sockat.ex.sweep_base(given)
    else:
        with raises_errno(outcome):
            sockat.ex.sweep_base(given)
    assert sorted(os.listdir(root)) == before


# A B owned by another uid is EPERM for sweep_base (run as root, which can
# make one).
@pytest.mark.skipif(not ROOT, reason="needs root to make a directory another uid owns")
def test_sweep_base_of_another_uids_b(tmp: TmpDirs) -> None:
    root = tmp.new()
    given = root + b"/b"
    os.mkdir(given, 0o711)
    os.chown(given, 65534, 65534)
    with raises_errno(errno.EPERM):
        sockat.ex.sweep_base(given)


# sweep() passes over in silence a place that fails the check on the way
# (writable by others, not sticky) and a B that fails its own (writable by
# others): their dead D stay, and nothing is raised.
def test_sweep_passes_over_places_and_b_failing_their_checks(tmp: TmpDirs) -> None:
    unsafe = tmp.new()
    os.chmod(unsafe, 0o777)
    loose = tmp.new()
    for place, b_mode in ((unsafe, 0o711), (loose, 0o777)):
        b = place + b"/" + uid_name()
        os.mkdir(b)
        os.chmod(b, b_mode)
        os.mkdir(b + b"/dead", 0o711)
    _place.sweep(env_at(unsafe, loose))
    assert listing(unsafe + b"/" + uid_name()) == [b"dead"]
    assert listing(loose + b"/" + uid_name()) == [b"dead"]


# B itself found without o+x (another process's B caught between its mkdirat
# 0700 and its chmod 0711, here: one left 0700) is looked at again on each
# usable_cross_uid call: once it is 0711, the same B (and D) carries a
# spelling other uids can follow.
def test_b_without_x_for_others_is_looked_at_again(tmp: TmpDirs) -> None:
    place = tmp.new()
    os.chmod(place, 0o755)
    b = place + b"/" + uid_name()
    os.mkdir(b)
    os.chmod(b, 0o700)
    env = env_at(place, proc=NO_PROC, thread_cwd=False, link_file=_place.LinkFile.NOT_TRIED)
    opts = _calls.Opts(usable_cross_uid=True)
    if IS_FREEBSD:
        # FreeBSD's row has at (no thread needed): the bind goes through it,
        # its spelling Dangling, rather than through a B others cannot reach.
        first = bind_in(env, long_path(tmp.new()), opts=opts)
        first.sock.close()
        assert first.spelling == sockat.ex.Dangling()
    else:
        with raises_errno(errno.ENAMETOOLONG):
            bind_in(env, long_path(tmp.new()), opts=opts)
    os.chmod(b, 0o711)
    done = bind_in(env, long_path(tmp.new()), opts=opts)
    done.sock.close()
    assert isinstance(done.spelling, sockat.ex.Linked)
    assert done.spelling.usable_cross_uid
    assert listing(place) == [uid_name()]


# Moving on to the next place because B could not be made at one (here: the
# first is occupied), the spelling's length is judged again at that place:
# where it does not fit, no B is made there and no later place is tried,
# even a shorter one; the reason says too long, and the occupied B.
def test_the_next_place_is_judged_again(tmp: TmpDirs) -> None:
    first = tmp.new()
    with open(first + b"/" + uid_name(), "wb"):
        pass
    deep = deep_dir(tmp.new(), 90)
    short = tmp.new()
    env = env_at(first, deep, short, proc=NO_PROC, thread_cwd=False, link_file=_place.LinkFile.NOT_TRIED)
    trail = Trail()
    if IS_FREEBSD:
        # at needs no thread: it makes B (for its D) where B can be had,
        # whatever the length.
        bind_in(env, long_path(tmp.new()), trail=trail).sock.close()
    else:
        with raises_errno(errno.ENAMETOOLONG):
            bind_in(env, long_path(tmp.new()), trail=trail)
    (why,) = [s.reason for s in trail.skipped if s.name == "link-dir"]
    assert "spelling too long" in why
    assert "occupied" in why
    if not IS_FREEBSD:
        assert listing(deep) == []
        assert listing(short) == []


# A B (and its D) removed while held is let go but its fds stay open for the
# life of the process (SPEC-routes): closed, their numbers would be used
# again and a live socket's procB spelling through them would lead into
# another directory. The next call makes B and D again.
def test_a_b_let_go_keeps_its_fds(tmp: TmpDirs) -> None:
    env = env_at(tmp.new(), proc=NO_PROC)
    bind_in(env, long_path(tmp.new())).sock.close()
    b = env.auto_base()
    assert b is not None
    d = b.dir(env.config.uid)
    assert d is not None
    old_b, old_d = b.fd, d.fd
    shutil.rmtree(b.real)
    bind_in(env, long_path(tmp.new())).sock.close()
    again = env.auto_base()
    assert again is not None
    assert again is not b
    os.fstat(old_b)
    os.fstat(old_d)
    assert again.fd not in (old_b, old_d)
    # A D removed alone is let go the same way.
    nd = again.dir(env.config.uid)
    assert nd is not None
    kept = nd.fd
    shutil.rmtree(again.real + b"/" + nd.name)
    bind_in(env, long_path(tmp.new())).sock.close()
    os.fstat(kept)
    fresh = again.dir(env.config.uid)
    assert fresh is not None
    assert fresh is not nd


# "/proc を使える" is judged per use (SPEC): the connect's row by
# /proc/self/fd, procB by /proc/<pid>/fd. A /proc whose self entries name the
# fds but whose <pid> entries do not (another PID namespace's) gives the
# proc-direct row and no procB.
def test_proc_is_judged_per_use(tmp: TmpDirs) -> None:
    from sockat import _routes

    proc = tmp.new() + b"/proc"
    os.makedirs(proc + b"/self/fd")
    for n in range(256):
        # Each entry names "/", the directory the check opens.
        os.symlink(b"/", proc + b"/self/fd/%d" % n)
    os.makedirs(proc + b"/%d/fd" % os.getpid())
    env = env_at(tmp.new(), proc=proc)
    assert _routes.proc_usable(env, pid=False)
    assert not _routes.proc_usable(env, pid=True)
    trail = Trail()
    done = bind_in(env, long_path(tmp.new()), trail=trail)
    done.sock.close()
    assert done.route == "link-dir"
    assert done.base != "proc"


# sweep() passes over in silence a B it cannot read (no permission), leaving
# what is in it.
@pytest.mark.skipif(ROOT, reason="root reads anything")
def test_sweep_passes_over_a_b_it_cannot_read(tmp: TmpDirs) -> None:
    place = tmp.new()
    b = place + b"/" + uid_name()
    os.mkdir(b, 0o711)
    os.mkdir(b + b"/Dead567890", 0o711)
    os.chmod(b, 0o311)
    try:
        _place.sweep(env_at(place))
    finally:
        os.chmod(b, 0o711)
    assert listing(b) == [b"Dead567890"]


# The places are told apart by their realpath's (st_dev, st_ino): $TMPDIR as
# /tmp itself, or as a symlink to it, is the same place as /tmp and is tried
# once.
def test_the_same_place_is_tried_once(tmp: TmpDirs) -> None:
    link = tmp.new() + b"/tmp-link"
    os.symlink(b"/tmp", link)
    for tmpdir in (b"/tmp", b"/tmp/", link):
        places = _place.default_places(os.geteuid(), None, tmpdir)
        assert [p.kind for p in places] == [Kind.TMP]
    other = tmp.new()
    assert [p.kind for p in _place.default_places(os.geteuid(), None, other)] == [Kind.TMP, Kind.TMPDIR]
