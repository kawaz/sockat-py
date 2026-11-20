"""B, D and E (hub SPEC-routes "B の作り方", "実体の置き場", "D と E",
SPEC "sweep"): where B is made and what is checked before it is used, D held
by a lock for the life of the process, the processes and threads that make
them at once, and ``sweep``."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from _support import (
    HAS_THREAD_ROUTE,
    PRELUDE,
    ROOT,
    TmpDirs,
    accept_ping,
    base_of,
    bind_in,
    connect_in,
    ds,
    env_at,
    is_socket,
    listing,
    long_path,
    raises_errno,
    run_child,
)

import sockat.ex
from sockat import _calls, _place
from sockat._place import Kind, State
from sockat._spelling import Trail


def uid_name() -> bytes:
    return b"sockat-%d" % os.getuid()


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


# A B already there is used when it passes the same checks; one that does
# not (others can write to it, or a symlink in its place) is left alone and a
# B of a random name sockat-<uid>.XXXXXXXXXX is made beside it.
@pytest.mark.parametrize("what", ["ours", "writable", "symlink", "file"])
def test_an_existing_b_is_checked(tmp: TmpDirs, what: str) -> None:
    place = tmp.new()
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
    env = env_at(place)
    real = base_of(env)
    if what == "ours":
        assert real == os.path.realpath(b)
    else:
        assert os.path.dirname(real) == os.path.realpath(place)
        assert os.path.basename(real).startswith(uid_name() + b".")
        assert len(os.path.basename(real)) == len(uid_name()) + 11
        assert os.path.lexists(b)
        assert listing(elsewhere) == []
        if what == "writable":
            assert mode(b) == 0o777


# Options.base is B itself: made there when missing (its parent must exist),
# used when there; D goes in it.
def test_options_base_is_b(tmp: TmpDirs) -> None:
    root = tmp.new()
    given = root + b"/b"
    bound = sockat.ex.bind(long_path(tmp.new()), sockat.ex.Options(base=given))
    with bound.socket:
        assert mode(given) == 0o711
        assert len(ds(given)) == 1
        assert bound.route in ("link-dir", "link-file", "thread-cwd", "at")
        if bound.route in ("link-dir", "link-file"):
            assert bound.base in ("proc", "base")
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
    env = env_at(closed, open_)
    done = bind_in(env, long_path(tmp.new()), opts=_calls.Opts(usable_cross_uid=True))
    with done.sock:
        if done.route in ("link-dir", "link-file"):
            assert isinstance(done.spelling, sockat.ex.Linked)
            assert done.spelling.usable_cross_uid
            assert done.base == "tmpdir" or done.base == "runtime"
            assert listing(open_) == [uid_name()]
        else:
            assert done.spelling == sockat.ex.Dangling()


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
            return Env(Config(places=(Place(Kind.TMP, {place!r}, b"sockat-%d" % os.getuid()),)), _place.state())
        _calls.bind(long_path({work!r} + b"/parent"), env=env())
        parent_d = _place.state().auto.dir(os.getuid()).name
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            _calls.bind(long_path({work!r} + b"/child"), env=env())
            os.write(w, _place.state().auto.dir(os.getuid()).name)
            os._exit(0)
        os.waitpid(pid, 0)
        still = _place.state().auto.dir(os.getuid()).name.decode()
        report(parent=parent_d.decode(), child=os.read(r, 100).decode(), still=still)
        """
    )
    assert out["parent"] != out["child"]
    assert out["still"] == out["parent"]


# The last place is the socket's directory: B there is named
# .sockat.XXXXXXXXXX, and the process keeps one per directory.
def test_b_in_the_socket_directory_is_the_last_place(tmp: TmpDirs) -> None:
    env = _place.Env(_place.Config(places=()), State())
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
