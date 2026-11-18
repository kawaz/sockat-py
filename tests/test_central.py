"""The central route (hub SPEC, DR-0012): a private directory D made in a
place and held by its fd, holding R, a symlink with a random name to the
realpath of the socket's directory, and the bind / connect of ``R/<name>``
from D's fd: through a thread's own cwd (Linux, Apple), ``bindat`` /
``connectat`` (FreeBSD), or ``/proc/self/fd/<fd of D>/R/<name>`` (Linux).

The paths are 600 bytes deep under /tmp unless a test says otherwise (so no
fallback of a connect is the relative one, and the directory's length does not
matter to the route). The configurations come from
:func:`_support.central_cfg`."""

from __future__ import annotations

import dataclasses
import errno
import os
import re
import socket
import stat
import sys
import threading
import time
import types
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from _support import (
    HAS_PROC,
    NAME_MAX_CENTRAL,
    NO_PLACE,
    NO_PROC,
    PROC,
    UNTOUCHED,
    TmpDirs,
    accept_ping,
    assert_empty,
    assert_far_from_cwd,
    assert_untouched,
    central_cfg,
    central_route_here,
    check_full_backlog,
    check_route,
    dir_id,
    forced,
    is_socket,
    kernel_sockname,
    listener_at,
    listing,
    mark_untouched,
    nest,
    parent,
    ping,
    place,
    raises_errno,
    run_child,
    stale_socket,
    thread_cwd_usable,
    with_name,
)

from sockat import _route, _sys
from sockat._route import (
    BIND,
    CONNECT,
    CONNECT_NONBLOCKING,
    OWN_DIR_PREFIX,
    OWN_DIR_PREFIX_IN_SOCKET_DIR,
    RANDOM_LEN,
    SOCKET_DIR,
    Config,
    OwnDir,
    OwnDirPlace,
    Route,
)
from sockat._sys import DIRECT_MAX, IS_APPLE, IS_FREEBSD, IS_LINUX

pytestmark = pytest.mark.skipif(central_route_here() is None, reason="no means of the central route here")

IS_ROOT = os.geteuid() == 0

NOBODY = 65534
"""Another user, for the root runs (docker's ``nobody``)."""

RANDOM = rb"[A-Za-z0-9]{%d}" % RANDOM_LEN


@dataclass(frozen=True)
class Means:
    """One means of the central route, forced: its name, the configuration
    (with ``own`` as the places for D) that takes it, and the route."""

    what: str
    cfg: Callable[[tuple[OwnDirPlace, ...]], Config]
    route: Route


def means_here() -> list[Means]:
    """Every means of the central route this system can use: a thread's own
    cwd (Linux where the system allows it, Apple), bindat (FreeBSD), /proc
    with the thread's own cwd turned off (Linux)."""
    found: list[Means] = []
    if IS_FREEBSD:
        found.append(Means("at", lambda own: central_cfg(own), Route.AT))
    if thread_cwd_usable():
        found.append(Means("thread-cwd", lambda own: central_cfg(own, proc=NO_PROC), Route.THREAD_CWD))
    if HAS_PROC:
        found.append(Means("proc", lambda own: central_cfg(own, thread_cwd=False), Route.PROC_OWN_DIR))
    return found


MEANS = means_here()
MEANS_IDS = [m.what for m in MEANS]


def far(tmp: TmpDirs, name: bytes = b"s.sock") -> bytes:
    p = nest(tmp.new(), 600, name)
    assert_far_from_cwd(p)
    return p


@dataclass
class Seen:
    """What :func:`spy` saw of one D right after it was made."""

    place: tuple[int, int]
    name: bytes
    mode: int
    owner: int
    entries: list[bytes]
    link: bytes
    target: bytes


def listing_of(own: OwnDir) -> list[bytes]:
    """What D holds, listed through a second fd opened by its name in the
    place (D's own fd may be ``O_PATH`` / ``O_SEARCH``, which cannot be
    listed)."""
    fd = os.open(own.name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=own.base_fd)
    try:
        return sorted(os.fsencode(e) for e in os.listdir(fd))
    finally:
        os.close(fd)


def spy(monkeypatch: pytest.MonkeyPatch) -> list[Seen]:
    """Record every D the route makes (where, its name, mode and owner, what
    it holds, R and where R points), as :meth:`OwnDir.make` returns it."""
    seen: list[Seen] = []
    real = OwnDir.make

    def make(*args: object, **kwargs: object) -> OwnDir | None:
        own = real(*args, **kwargs)  # type: ignore[arg-type]
        if own is not None:
            st = os.fstat(own.fd)
            base = os.fstat(own.base_fd)
            seen.append(
                Seen(
                    place=(base.st_dev, base.st_ino),
                    name=own.name,
                    mode=stat.S_IMODE(st.st_mode),
                    owner=st.st_uid,
                    entries=listing_of(own),
                    link=own.link,
                    target=os.readlink(own.link, dir_fd=own.fd),
                )
            )
        return own

    monkeypatch.setattr(OwnDir, "make", make)
    return seen


def assert_used(d: bytes) -> None:
    """Something was created in ``d`` and removed again since
    :func:`_support.mark_untouched` (its mtime moved), and nothing is left."""
    assert os.stat(d).st_mtime != UNTOUCHED, f"nothing was made in {d!r}"
    assert_empty(d)


# ----- every means -----


# Each means of the central route, forced, gets every check of a route
# (check_route: the socket at the given path, data both ways, blocking and
# nonblocking connects, close-on-exec, the peer's pid is this process's, the
# process cwd unchanged, the path reported as the address while the kernel's
# address fits sun_path, asyncio serving and cleaning up as for the
# standard's) and the nonblocking connect against a full backlog. The place
# for D is empty after every step, though D was made there (its mtime moved).
@pytest.mark.parametrize("means", MEANS, ids=MEANS_IDS)
def test_every_means_of_the_central_route(tmp: TmpDirs, means: Means) -> None:
    p = far(tmp)
    d_place = tmp.new()
    mark_untouched(d_place)
    f = forced(means.what, p, means.cfg((d_place,)), means.route)
    check_route(f, [d_place])
    check_full_backlog(f)
    assert_used(d_place)


# What is made, seen right after it is made, for each means: in the place,
# a directory `sockat.` and 10 letters or digits, owned by the effective uid,
# mode exactly 0700, holding only R (10 letters or digits, no `sockat.`), a
# symlink to the realpath of the socket's directory (here reached through a
# symlink, so the realpath differs from the path given). The spelling the
# kernel holds as the listener's address is `R/<name>`, or behind
# `/proc/self/fd/<n>/` for /proc. Afterwards the place is empty.
@pytest.mark.parametrize("means", MEANS, ids=MEANS_IDS)
def test_d_is_private_and_r_points_to_the_realpath_of_the_directory(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, means: Means
) -> None:
    t = tmp.new()
    real_dir = parent(nest(t, 600))
    os.symlink(real_dir, t + b"/link")
    p = t + b"/link/s.sock"
    d_place = tmp.new()
    seen = spy(monkeypatch)
    listener, r = _route.run_long(BIND, p, means.cfg((d_place,)))
    with listener:
        assert r is means.route
        assert is_socket(real_dir + b"/s.sock")
        [d] = seen
        assert d.place == dir_id(d_place)
        assert re.fullmatch(re.escape(OWN_DIR_PREFIX) + RANDOM, d.name), d.name
        assert (d.mode, d.owner) == (0o700, os.geteuid())
        assert re.fullmatch(RANDOM, d.link), d.link
        assert d.entries == [d.link]
        assert d.target == os.fsencode(os.path.realpath(real_dir))
        kernel = os.fsencode(kernel_sockname(listener))
        if means.route is Route.PROC_OWN_DIR:
            assert re.fullmatch(rb"/proc/self/fd/\d+/" + re.escape(d.link) + rb"/s\.sock", kernel), kernel
        else:
            assert kernel == d.link + b"/s.sock", kernel
    assert_empty(d_place)


# ----- the cwd -----


# The thread-cwd means changes the cwd of its own throwaway thread only: a
# calling thread with a cwd of its own (given the same way, by
# enter_dir_in_this_thread) still has that cwd after binding and connecting,
# and the process cwd (every other thread's) is unchanged too. A
# pthread_fchdir_np(-1) in the calling thread to go back would have dropped
# the calling thread's own cwd instead.
@pytest.mark.skipif(not thread_cwd_usable(), reason="the system does not give a thread a cwd of its own here")
def test_the_calling_threads_own_cwd_is_left_alone(tmp: TmpDirs) -> None:
    p = far(tmp)
    mine = tmp.new()
    process = dir_id(".")
    c = central_cfg(proc=NO_PROC)
    seen: dict[str, object] = {}

    def caller() -> None:
        fd = os.open(mine, os.O_RDONLY)
        try:
            assert _sys.enter_dir_in_this_thread(fd)
        finally:
            os.close(fd)
        seen["before"] = dir_id(".")
        listener, r = _route.run_long(BIND, p, c)
        with listener:
            client, r2 = _route.run_long(CONNECT, p, c)
            with client:
                accept_ping(listener, client)
        seen["routes"] = (r, r2)
        seen["after"] = dir_id(".")

    th = threading.Thread(target=caller)
    th.start()
    th.join()
    assert seen["routes"] == (Route.THREAD_CWD, Route.THREAD_CWD)
    assert seen["before"] == seen["after"] == dir_id(mine)
    assert dir_id(".") == process


# The process cwd never changes while many binds and connects run through
# the central route from several threads: threads that keep checking getcwd
# and the identity of "." the whole time never see a change.
@pytest.mark.parametrize("means", MEANS, ids=MEANS_IDS)
def test_the_process_cwd_never_changes(tmp: TmpDirs, means: Means) -> None:
    cwd, cwd_id = os.getcwd(), dir_id(".")
    stop = threading.Event()
    checks = [0]
    errors: list[BaseException] = []
    c = means.cfg((SOCKET_DIR,))

    def watch() -> None:
        while not stop.is_set():
            if os.getcwd() != cwd or dir_id(".") != cwd_id:
                errors.append(AssertionError("the process cwd changed"))
                return
            checks[0] += 1

    def work(i: int) -> None:
        try:
            t = tmp.new()
            for round_ in range(20):
                p = nest(t, 600, f"{i}-{round_}.sock".encode())
                listener, r = _route.run_long(BIND, p, c)
                with listener:
                    assert r is means.route
                    client, r = _route.run_long(CONNECT, p, c)
                    with client:
                        assert r is means.route
                        server, _ = listener.accept()
                        with server:
                            ping(server, client)
        except BaseException as e:
            errors.append(e)

    watchers = [threading.Thread(target=watch) for _ in range(2)]
    workers = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for th in watchers + workers:
        th.start()
    for th in workers:
        th.join()
    stop.set()
    for th in watchers:
        th.join()
    assert errors == []
    assert checks[0] > 0


# ----- the spelling the kernel keeps -----


# The spelling the kernel keeps for the listener (socket.socket.getsockname:
# `R/<name>`, or `/proc/self/fd/<n>/R/<name>`) names nothing once the call has
# returned, whatever it is resolved against: stat and unlink of it fail with
# FileNotFoundError, and a file of the socket's name in the process cwd is
# left as it was (the spelling of a bind by the file name alone would have hit
# it). Runs in a child process whose cwd holds that file; see the scenario.
def test_the_kernels_spelling_names_nothing_from_the_cwd(tmp: TmpDirs) -> None:
    t = tmp.new()
    p = far(tmp)
    out = run_child("spelling_namesakes", t, {"PATH": p, "MEANS": ",".join(MEANS_IDS).encode()})
    assert out == {"checked": ",".join(MEANS_IDS)}


# The /proc spelling `/proc/self/fd/<n>/R/<name>` names nothing even once the
# number <n> (D's fd, closed when the bind returns) is taken by another
# directory: files are opened until one gets the number, here a directory
# holding a file of the socket's name. stat of the spelling is then
# FileNotFoundError (R is not in that directory) and the file there is left
# alone. The spelling without R, `/proc/self/fd/<n>/<name>` (what a bind
# through /proc on the socket's directory would leave), names that file: the
# premise of the test.
@pytest.mark.skipif(not HAS_PROC, reason="the /proc means is Linux's")
def test_the_proc_spelling_names_nothing_when_its_fd_number_is_reused(tmp: TmpDirs) -> None:
    p = far(tmp)
    other = tmp.new()
    with open(other + b"/s.sock", "wb") as f:
        f.write(b"unrelated")
    listener, r = _route.run_long(BIND, p, central_cfg(thread_cwd=False))
    with listener:
        assert r is Route.PROC_OWN_DIR
        kernel = os.fsencode(kernel_sockname(listener))
        m = re.fullmatch(rb"/proc/self/fd/(\d+)/(" + RANDOM + rb")/s\.sock", kernel)
        assert m is not None, kernel
        n = int(m.group(1))
        opened: list[int] = []
        try:
            while not opened or opened[-1] != n:
                opened.append(os.open(other, os.O_RDONLY | os.O_DIRECTORY))
                assert opened[-1] <= n, "the premise: D's closed fd number is free"
            assert os.path.exists(f"/proc/self/fd/{n}/s.sock"), "the premise: the number names the other directory"
            with pytest.raises(FileNotFoundError):
                os.stat(kernel)
            with pytest.raises(FileNotFoundError):
                os.unlink(kernel)
        finally:
            for fd in opened:
                os.close(fd)
    with open(other + b"/s.sock", "rb") as f:
        assert f.read() == b"unrelated"


# ----- the places for D -----


def fallback_route_here(req: _route.Request) -> Route:
    """The fallback a connect to a path far from the cwd takes here with a
    short safe place for the symlink and the real /proc: connectat on
    FreeBSD, /proc on Linux, the symlink elsewhere."""
    if IS_FREEBSD:
        return Route.AT_DIR
    if HAS_PROC:
        return Route.PROC_FD
    return Route.SYMLINK_TO_FILE


# D goes in the first place where it can be made, and the places it cannot be
# made in (missing, a file, a directory the user may not write to) are passed
# over with nothing made there. The socket's directory is the last place, and
# D is named `.sockat.` and 10 letters or digits there. Where D can be made
# nowhere, nothing is made, the bind is ENAMETOOLONG (the fork route's place)
# and a connect takes its fallback. Root writes anywhere, so the unwritable
# place is checked only for other users (the docker nobody run, macOS).
@pytest.mark.parametrize("means", MEANS, ids=MEANS_IDS)
def test_d_goes_in_the_first_place_where_it_can_be_made(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, means: Means
) -> None:
    t = tmp.new()
    file = t + b"/file"
    with open(file, "wb"):
        pass
    bad: list[bytes] = [NO_PLACE, file]
    if not IS_ROOT:
        bad.append(place(t, b"unwritable", 0o500))
    good = tmp.new()
    seen = spy(monkeypatch)
    p = far(tmp)
    for d in [*bad[1:], good]:
        mark_untouched(d)

    listener, r = _route.run_long(BIND, p, means.cfg((*bad, good)))
    listener.close()
    assert r is means.route
    assert [s.place for s in seen] == [dir_id(good)]
    assert_used(good)
    for d in bad[1:]:
        assert_untouched(d)

    seen.clear()
    q = with_name(p, b"q.sock")
    listener, r = _route.run_long(BIND, q, means.cfg((*bad, SOCKET_DIR)))
    with listener:
        assert r is means.route
        [s] = seen
        assert s.place == dir_id(parent(p))
        assert re.fullmatch(re.escape(OWN_DIR_PREFIX_IN_SOCKET_DIR) + RANDOM, s.name), s.name
        assert sorted(listing(parent(p))) == sorted([b"s.sock", b"q.sock"])

    seen.clear()
    links = tmp.new()
    z = with_name(p, b"z.sock")
    c = central_cfg(tuple(bad), [links], thread_cwd=means.route is Route.THREAD_CWD)
    before = listing(parent(p))
    with raises_errno(errno.ENAMETOOLONG):
        _route.run_long(BIND, z, c)
    assert listing(parent(p)) == before
    with listener_at(z) as listener:
        for req in (CONNECT, CONNECT_NONBLOCKING):
            client, r = _route.run_long(req, z, c)
            with client:
                assert r is fallback_route_here(req), req
                accept_ping(listener, client)
    assert seen == []
    for d in bad[1:]:
        assert_untouched(d)


# The places for D read from the environment: ${TMPDIR:-/tmp}, then /tmp
# unless TMPDIR is /tmp itself, then the socket's directory. A relative TMPDIR
# is kept as it is (it is opened against the cwd, once). The places for a
# connect's symlink: TMPDIR when it is absolute and not /tmp, then /tmp.
def test_the_places_read_from_the_environment() -> None:
    def own(tmpdir: bytes | None) -> tuple[OwnDirPlace, ...]:
        return Config.from_tmpdir(tmpdir).own_dir_places

    def links(tmpdir: bytes | None) -> tuple[bytes, ...]:
        return Config.from_tmpdir(tmpdir).link_places

    assert own(None) == own(b"") == (b"/tmp", SOCKET_DIR)
    assert own(b"/var/x/") == (b"/var/x/", b"/tmp", SOCKET_DIR)
    assert own(b"rel") == (b"rel", b"/tmp", SOCKET_DIR)
    for same in (b"/tmp", b"/tmp/", b"//tmp/."):
        assert own(same) == (same, SOCKET_DIR)
        assert links(same) == (b"/tmp",)
    assert links(None) == links(b"") == links(b"rel") == (b"/tmp",)
    assert links(b"/var/x/") == (b"/var/x/", b"/tmp")
    c = Config.from_tmpdir(None)
    assert c.wide is IS_APPLE
    assert c.proc_self_fd == PROC
    assert c.thread_cwd is _sys.HAS_THREAD_CWD


# Through the environment as the public API reads it (Config.from_env), in a
# child process because it sets TMPDIR: D is made in TMPDIR when it can be,
# whether TMPDIR is absolute, relative to the cwd, or 600 bytes deep (the
# place's length never goes into the spelling); and in /tmp when TMPDIR does
# not exist. The child reports the places D was made in, and the route.
@pytest.mark.parametrize("kind", ["absolute", "relative", "deep", "missing"])
def test_d_goes_in_tmpdir_then_tmp(tmp: TmpDirs, kind: str) -> None:
    t = tmp.new()
    p = far(tmp)
    if kind == "absolute":
        tmpdir, where = tmp.new(), None
    elif kind == "relative":
        os.mkdir(t + b"/rel")
        tmpdir, where = b"rel", t + b"/rel"
    elif kind == "deep":
        tmpdir = parent(nest(tmp.new(), 600, b"x"))
        where = None
    else:
        tmpdir, where = t + b"/missing", b"/tmp"
    where = where or tmpdir
    out = run_child("own_dir_where", t, {"PATH": p}, env={"TMPDIR": tmpdir})
    want = central_route_here()
    assert want is not None
    assert out["route"] == want.value, out
    st = os.stat(where)
    assert out["places"] == f"{st.st_dev}:{st.st_ino}", out
    if kind != "missing":
        assert_empty(where)


# ----- a D that is not ours -----


REPLACEMENTS = ["mode-755", "mode-750", "mode-701", "renamed", "renamed-holding-r", "symlink", "owner"]


def replacing_mkdir(kind: str, base: bytes, aside: bytes) -> Callable[[int, bytes], None]:
    """A ``mkdir_at`` that makes D and then, before the route opens it,
    changes what is at the name (in the place ``base``), as another user (or
    root) could:

    * ``mode-<octal>``: its mode becomes that (some permission for group or
      others);
    * ``owner``: it is given to another user (root only);
    * ``renamed``: it is renamed to ``<aside>/orig`` and an empty directory
      of mode 0777 takes its name;
    * ``renamed-holding-r``: the same, the new directory holding a file of
      its own;
    * ``symlink``: it is renamed to ``<aside>/orig`` and a symlink to
      ``<aside>/decoy`` (the user's own, mode 0700, which would pass the
      check if the symlink were followed) takes its name.
    """

    def make(base_fd: int, name: bytes) -> None:
        os.mkdir(name, 0o700, dir_fd=base_fd)
        d = base + b"/" + name
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
        place(base, name, 0o777)
        if kind == "renamed-holding-r":
            with open(d + b"/theirs", "wb") as f:
                f.write(b"theirs")

    return make


# A D replaced between its creation and the open is not used and is left as
# the replacement made it: the check on the opened fd (a directory, not a
# symlink, owned by the effective uid, no permission for group or others)
# fails, or the open itself (O_NOFOLLOW on a symlink). Nothing is created in
# it and nothing in it is removed; the place counts as one where D cannot be
# made, and the next place is used for the bind, which lands at the path. The
# replacement is made once, for the bind; the connect after it uses the first
# place as usual (and leaves it as it found it). Afterwards the first place
# holds only the replacement:
#
# * a mode or owner change, renamed: the directory at the name as it was
#   left, empty (the one made, renamed away, stays empty too);
# * renamed, holding a file: left with its file;
# * a symlink: left as it is, and the directory it points to is not used.
@pytest.mark.parametrize("kind", REPLACEMENTS)
@pytest.mark.parametrize("means", MEANS, ids=MEANS_IDS)
def test_a_d_replaced_before_it_is_opened_is_not_used(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, kind: str, means: Means
) -> None:
    if kind == "owner" and not IS_ROOT:
        pytest.skip("only root can give a directory to another user")
    p = far(tmp)
    first = tmp.new()
    second = tmp.new()
    aside = tmp.new()
    real = _route.mkdir_at
    first_id = dir_id(first)
    replaced: list[bytes] = []

    def mkdir_at(base_fd: int, name: bytes) -> None:
        st = os.fstat(base_fd)
        if (st.st_dev, st.st_ino) == first_id and not replaced:
            replaced.append(name)
            replacing_mkdir(kind, first, aside)(base_fd, name)
        else:
            real(base_fd, name)

    monkeypatch.setattr(_route, "mkdir_at", mkdir_at)
    c = means.cfg((first, second))
    mark_untouched(second)
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is means.route
        assert is_socket(p)
        assert os.stat(second).st_mtime != UNTOUCHED, "the second place was not used for the bind"
        client, r = _route.run_long(CONNECT, p, c)
        with client:
            assert r is means.route
            accept_ping(listener, client)
    assert_empty(second)
    [entry] = listing(first)
    assert replaced == [entry]
    at = first + b"/" + entry
    if kind == "symlink":
        assert os.readlink(at) == aside + b"/decoy"
        assert listing(aside + b"/decoy") == []
        assert_untouched(aside + b"/decoy")
    elif kind == "renamed-holding-r":
        assert listing(at) == [b"theirs"]
    else:
        assert listing(at) == []
    if kind.startswith("renamed") or kind == "symlink":
        assert listing(aside + b"/orig") == []


# Once D is open its name no longer matters: renamed away and replaced by
# another directory while the bind runs, the bind still goes through D's fd
# and lands at the path. Afterwards R is removed through the fd (the D made,
# now out of reach under its new name, is left empty), and the directory at
# the name is not removed: the name no longer refers to D.
@pytest.mark.parametrize("means", MEANS, ids=MEANS_IDS)
def test_a_d_replaced_after_it_is_opened_changes_nothing(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, means: Means
) -> None:
    p = far(tmp)
    base = tmp.new()
    aside = tmp.new()
    real = OwnDir.make
    moved: list[bytes] = []

    def make(*args: object, **kwargs: object) -> OwnDir | None:
        own = real(*args, **kwargs)  # type: ignore[arg-type]
        assert own is not None
        os.rename(base + b"/" + own.name, aside + b"/" + own.name)
        place(base, own.name, 0o700)
        moved.append(own.name)
        return own

    monkeypatch.setattr(OwnDir, "make", make)
    listener, r = _route.run_long(BIND, p, means.cfg((base,)))
    with listener:
        assert r is means.route
        assert is_socket(p)
    [name] = moved
    assert listing(base) == [name]
    assert listing(base + b"/" + name) == []
    assert listing(aside) == [name]
    assert listing(aside + b"/" + name) == []


# D's name is drawn afresh when taken (by anything; here a symlink, left as it
# was), and after 16 taken names the place counts as one where D cannot be
# made: nothing is created in it and the next place is used. The names come
# from a stand-in generator so the collision is certain.
@pytest.mark.parametrize("means", MEANS, ids=MEANS_IDS)
def test_a_taken_name_for_d_is_drawn_again_and_16_end_the_place(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, means: Means
) -> None:
    p = far(tmp)
    crowded = tmp.new()
    second = tmp.new()
    taken = OWN_DIR_PREFIX + b"A" * RANDOM_LEN
    free = OWN_DIR_PREFIX + b"B" * RANDOM_LEN
    os.symlink(b"/nonexistent", crowded + b"/" + taken)
    drawn: list[bytes] = []
    real = _route.random_name
    picks = iter([taken, free])

    def two(prefix: bytes = OWN_DIR_PREFIX) -> bytes:
        n = next(picks)
        drawn.append(n)
        return n

    monkeypatch.setattr(_route, "random_name", two)
    listener, r = _route.run_long(BIND, p, means.cfg((crowded,)))
    listener.close()
    assert r is means.route
    assert drawn == [taken, free]
    assert listing(crowded) == [taken]
    assert os.readlink(crowded + b"/" + taken) == b"/nonexistent"

    drawn.clear()

    def taken_16_times(prefix: bytes = OWN_DIR_PREFIX) -> bytes:
        n = taken if len(drawn) < 16 else real(prefix)
        drawn.append(n)
        return n

    monkeypatch.setattr(_route, "random_name", taken_16_times)
    mark_untouched(crowded)
    q = with_name(p, b"q.sock")
    listener, r = _route.run_long(BIND, q, means.cfg((crowded, second)))
    listener.close()
    assert r is means.route
    assert drawn[:16] == [taken] * 16
    assert len(drawn) == 17
    assert_untouched(crowded)
    assert_empty(second)


# The directory is made with mode 0700, which a umask may cut down. A umask
# that takes owner bits off leaves no permission for group or others, so the
# check passes, and D is set to 0700 before R is created in it: the bind gets
# through. D is opened in a way that needs no read permission on it (Linux
# O_PATH, Apple O_SEARCH), and set to 0700 through /proc (Linux with /proc),
# by fchmod (Apple), or, on Linux without /proc, by fchmod on D opened again
# by its name O_RDONLY: that needs the owner's read bit, so for a user other
# than root a umask taking it off (0477, 0777) leaves D that cannot be set to
# 0700; D is then removed again and the place counts as one where D cannot be
# made (with no other place, the bind is ENAMETOOLONG). Either way nothing is
# left in the place (no D piling up, one per call). Runs in a child process
# because the umask is process-wide.
#
# Not checked: where D is opened O_SEARCH (Apple), a umask taking the owner's
# search bit off (0777, 0177) stops the open itself for a user other than
# root, and D is left in the place (a D that cannot be opened cannot be told
# to be ours); on the other systems (O_RDONLY) the same for the read bit.
@pytest.mark.parametrize("umask", ["077", "022", "277", "477", "777"])
@pytest.mark.parametrize("means", MEANS, ids=MEANS_IDS)
def test_a_umask_taking_owner_bits_off_leaves_nothing_in_the_place(tmp: TmpDirs, umask: str, means: Means) -> None:
    taken = int(umask, 8)
    if not IS_ROOT and ((IS_APPLE and taken & 0o100) or (not IS_LINUX and not IS_APPLE and taken & 0o400)):
        pytest.skip("D cannot be opened under this umask here (reported to the SPEC)")
    # The thread-cwd means is forced with /proc made to look absent, so on
    # Linux D's mode is set through the reopening by name.
    reopened = IS_LINUX and means.route is not Route.PROC_OWN_DIR
    fails = reopened and not IS_ROOT and bool(taken & 0o400)
    p = far(tmp)
    d_place = tmp.new()
    args = {"PATH": p, "PLACE": d_place, "UMASK": umask.encode(), "MEANS": means.what.encode()}
    out = run_child("bind_with_umask", b"/", args)
    assert out == ({"errno": str(errno.ENAMETOOLONG)} if fails else {"route": means.route.value}), out
    assert_empty(d_place)


# A D that passed the check but cannot be set to 0700 (stood in for by a
# set_own_dir_mode that fails) counts as a place where D cannot be made: that
# D is removed again and the next place is used.
@pytest.mark.parametrize("means", MEANS, ids=MEANS_IDS)
def test_a_d_that_cannot_be_set_to_0700_is_removed_and_the_next_place_used(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, means: Means
) -> None:
    p = far(tmp)
    first = tmp.new()
    second = tmp.new()
    real = _route.set_own_dir_mode
    first_id = dir_id(first)

    def set_mode(fd: int, base_fd: int, name: bytes, proc_self_fd: bytes) -> None:
        st = os.fstat(base_fd)
        if (st.st_dev, st.st_ino) == first_id:
            raise PermissionError(errno.EACCES, "stand-in")
        real(fd, base_fd, name, proc_self_fd)

    monkeypatch.setattr(_route, "set_own_dir_mode", set_mode)
    for d in (first, second):
        mark_untouched(d)
    listener, r = _route.run_long(BIND, p, means.cfg((first, second)))
    listener.close()
    assert r is means.route
    assert_used(first)
    assert_used(second)


# On Linux without /proc, D's mode is set through D opened again by its name,
# and only when that is the directory D's fd holds: given the name of another
# directory, set_own_dir_mode refuses (OSError) and leaves that directory's
# mode as it was.
@pytest.mark.skipif(not IS_LINUX, reason="the reopening by name is Linux's (O_PATH without /proc)")
def test_the_reopening_by_name_must_find_d_itself(tmp: TmpDirs) -> None:
    if sys.platform != "linux":
        return
    base = tmp.new()
    mine = place(base, b"mine", 0o500)
    place(base, b"other", 0o500)
    base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    fd = os.open(mine, os.O_PATH | os.O_DIRECTORY)
    try:
        with raises_errno(errno.ESTALE):
            _route.set_own_dir_mode(fd, base_fd, b"other", NO_PROC)
        assert stat.S_IMODE(os.stat(base + b"/other").st_mode) == 0o500
        _route.set_own_dir_mode(fd, base_fd, b"mine", NO_PROC)
        assert stat.S_IMODE(os.stat(mine).st_mode) == 0o700
    finally:
        os.close(fd)
        os.close(base_fd)


# ----- the means cannot be used -----


# Where the thread's own cwd cannot be used, the next means is tried in the
# same D: /proc on Linux, nothing on Apple (the bind is ENAMETOOLONG and a
# connect takes its fallback, the symlink here). Stood in for at the boundary
# (enter_dir_in_this_thread answering False, as under a seccomp filter or a
# refused unshare) and by a thread that cannot be started. D was made (the
# pre-check cannot see the refusal) and is removed again.
@pytest.mark.skipif(not thread_cwd_usable(), reason="the system does not give a thread a cwd of its own here")
@pytest.mark.parametrize("how", ["refused", "no-thread"])
def test_where_the_threads_cwd_cannot_be_used_the_next_means_is_tried(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    p = far(tmp)
    d_place = tmp.new()
    links = tmp.new()
    if how == "refused":
        monkeypatch.setattr(_route, "enter_dir_in_this_thread", lambda _fd: False)
    else:

        class NoThread:
            def __init__(self, **_: object) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("can't start new thread")

        monkeypatch.setattr("sockat._route.threading", types.SimpleNamespace(Thread=NoThread, Lock=threading.Lock))
    c = central_cfg((d_place,), [links])
    mark_untouched(d_place)
    if HAS_PROC:
        listener, r = _route.run_long(BIND, p, c)
        with listener:
            assert r is Route.PROC_OWN_DIR
            client, r = _route.run_long(CONNECT, p, c)
            with client:
                assert r is Route.PROC_OWN_DIR
                accept_ping(listener, client)
    else:
        with raises_errno(errno.ENAMETOOLONG):
            _route.run_long(BIND, p, c)
        assert not os.path.lexists(p)
        with listener_at(p) as listener:
            client, r = _route.run_long(CONNECT, p, c)
            with client:
                assert r is fallback_route_here(CONNECT)
                accept_ping(listener, client)
    assert_used(d_place)
    assert_empty(links)


# A file name too long for `R/<name>` in a sockaddr (NAME_MAX_CENTRAL + 1
# bytes) leaves the central route without a means before anything is made: the
# place for D stays untouched, the bind is ENAMETOOLONG with nothing created,
# and a connect takes its fallback. NAME_MAX_CENTRAL bytes still go through
# the thread's own cwd or bindat. Through /proc the limit is shorter
# (`/proc/self/fd/<n>/` in front): a name of 70 bytes goes, one of 85 does
# not.
@pytest.mark.parametrize("means", MEANS, ids=MEANS_IDS)
def test_a_file_name_too_long_for_the_central_route_makes_nothing(tmp: TmpDirs, means: Means) -> None:
    t = tmp.new()
    d_place = tmp.new()
    links = tmp.new()
    fits, too_long = (70, 85) if means.route is Route.PROC_OWN_DIR else (NAME_MAX_CENTRAL, NAME_MAX_CENTRAL + 1)
    c = dataclasses.replace(means.cfg((d_place,)), link_places=(links,))
    p = nest(t, 600, b"f" * fits)
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is means.route
        client, r = _route.run_long(CONNECT, p, c)
        with client:
            assert r is means.route
            accept_ping(listener, client)
    q = nest(t, 600, b"g" * too_long)
    mark_untouched(d_place)
    before = listing(parent(q))
    with raises_errno(errno.ENAMETOOLONG):
        _route.run_long(BIND, q, c)
    assert listing(parent(q)) == before
    assert_untouched(d_place)
    with listener_at(q) as listener:
        client, r = _route.run_long(CONNECT, q, c)
        with client:
            assert r is not means.route
            accept_ping(listener, client)
    assert_untouched(d_place)


# ----- failures -----


# A bind / connect that actually ran through the central route is the result:
# ENOENT for a missing socket, EADDRINUSE for an existing file, ECONNREFUSED
# for a socket nobody listens on come back as they are, and nothing is tried
# after them: no second place for D (it stays untouched), no fallback (the
# place for a connect's symlink stays untouched), no other means. D is
# removed in the failure too.
@pytest.mark.parametrize("means", MEANS, ids=MEANS_IDS)
def test_a_failure_through_the_central_route_is_not_retried(tmp: TmpDirs, means: Means) -> None:
    p = far(tmp)
    first = tmp.new()
    second = tmp.new()
    links = tmp.new()
    c = dataclasses.replace(means.cfg((first, second)), link_places=(links,))
    stale = with_name(p, b"stale.sock")
    stale_socket(stale)
    for d in (second, links):
        mark_untouched(d)
    for req in (CONNECT, CONNECT_NONBLOCKING):
        with pytest.raises(FileNotFoundError):
            _route.run_long(req, p, c)
        with pytest.raises(ConnectionRefusedError):
            _route.run_long(req, stale, c)
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is means.route
        with raises_errno(errno.EADDRINUSE):
            _route.run_long(BIND, p, c)
    with open(with_name(p, b"file"), "wb"):
        pass
    with raises_errno(errno.EADDRINUSE):
        _route.run_long(BIND, with_name(p, b"file"), c)
    assert_empty(first)
    for d in (second, links):
        assert_untouched(d)


# A missing directory is ENOENT, before any length is judged and before D is
# made (the directory is opened first): for the bind and both connects,
# whatever the file name's length (6 bytes; 254, longer than any sockaddr
# carries). Nothing is made in the place for D.
def test_a_missing_directory_is_enoent_before_anything_is_made(tmp: TmpDirs) -> None:
    t = tmp.new()
    directory = parent(nest(t, 600))
    d_place = tmp.new()
    mark_untouched(d_place)
    for n in (6, 254):
        missing = directory + b"/nope/" + b"n" * n
        for req in (BIND, CONNECT, CONNECT_NONBLOCKING):
            with pytest.raises(FileNotFoundError):
                _route.run_long(req, missing, central_cfg((d_place,)))
    assert listing(directory) == []
    assert_untouched(d_place)


# The realpath of the socket's directory is taken first, with the stat
# (SPEC): when it cannot be resolved, that error is raised as it is, before
# any length is judged or D is made, rather than the central route counting
# as unusable and the call ending in ENAMETOOLONG. Stood in for by a realpath
# that fails; nothing is made in the place for D.
def test_a_realpath_that_cannot_be_taken_is_the_error_it_is(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    p = nest(tmp.new(), 600)
    d_place = tmp.new()
    mark_untouched(d_place)

    def realpath(path: object, *, strict: bool = False) -> str:
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))

    monkeypatch.setattr(os.path, "realpath", realpath)
    for req in (BIND, CONNECT, CONNECT_NONBLOCKING):
        with pytest.raises(PermissionError):
            _route.run_long(req, p, central_cfg((d_place,)))
    assert_untouched(d_place)


# A removed cwd is not a directory to bind / connect in (SPEC: the realpath of
# the socket's directory is taken first, and one that cannot be taken is the
# error): with the cwd removed and $PWD unset, `./` + a long file name is
# ENOENT for the bind and both connects, and nothing is made in the place for
# D. macOS's getcwd still spells a removed cwd, so a realpath that took that
# spelling without checking it would go on. Run in a child process (the cwd
# is the process's).
def test_a_removed_cwd_is_enoent_and_nothing_is_made(tmp: TmpDirs) -> None:
    cwd = tmp.new() + b"/gone"
    os.mkdir(cwd)
    place = tmp.new()
    got = run_child("deleted_cwd", cwd, args={"PLACE": place})
    assert set(got.values()) == {str(errno.ENOENT)}, got
    assert listing(place) == []


# The socket's directory is looked up by stat, not by opening it (SPEC): one
# that cannot be opened (as a directory without read permission cannot be
# where it is opened O_RDONLY, stood in for here by an open that fails) still
# takes the routes that need no fd of it. The bind and both connects reach
# the socket through D in the first place, and nothing is made in the socket's
# directory. A path whose directory part is a file is ENOTDIR before anything
# is made.
def test_a_socket_directory_that_cannot_be_opened_leaves_the_routes_without_its_fd(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = nest(tmp.new(), 600)
    directory = parent(p)
    d_place = tmp.new()
    real_open_dir = _route.open_dir

    def open_dir(d: bytes) -> int:
        if d == directory:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        return real_open_dir(d)

    monkeypatch.setattr(_route, "open_dir", open_dir)
    cfg = central_cfg((d_place,))
    listener, _ = _route.run_long(BIND, p, cfg)
    with listener:
        for req in (CONNECT, CONNECT_NONBLOCKING):
            client, _ = _route.run_long(req, p, cfg)
            with client:
                accept_ping(listener, client)
    assert listing(directory) == [os.path.basename(p)]
    assert listing(d_place) == []
    a_file = tmp.new() + b"/file"
    with open(a_file, "wb"):
        pass
    for req in (BIND, CONNECT):
        with pytest.raises(NotADirectoryError):
            _route.run_long(req, a_file + b"/" + b"n" * 200, cfg)


# The socket's directory needs only search and write permission, as for a
# plain bind by path: it is opened O_PATH (Linux) / O_SEARCH (Apple), and D
# lives elsewhere. A directory of mode 0300 cannot be opened O_RDONLY, yet the
# central route binds and connects in it. Root bypasses permission checks, so
# as root the test cannot tell anything. FreeBSD opens the directory O_RDONLY
# (the SPEC's way there).
@pytest.mark.skipif(IS_ROOT, reason="root bypasses the permission under test")
@pytest.mark.skipif(IS_FREEBSD, reason="the directory is opened O_RDONLY there")
@pytest.mark.parametrize("means", MEANS, ids=MEANS_IDS)
def test_the_central_route_needs_no_read_permission_on_the_directory(tmp: TmpDirs, means: Means) -> None:
    p = far(tmp)
    os.chmod(parent(p), 0o300)
    with pytest.raises(PermissionError):
        os.open(parent(p), os.O_RDONLY)
    c = means.cfg((tmp.new(),))
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is means.route
        client, r = _route.run_long(CONNECT, p, c)
        with client:
            assert r is means.route
            accept_ping(listener, client)


# When the wait for the thread-cwd thread is interrupted (an exception such
# as KeyboardInterrupt out of join), the route still waits for the thread to
# end before it returns, so the central route does not remove D and close its
# fd while the thread is about to enter it (a closed fd, or one reused for an
# unrelated directory, where the thread would then bind). The interruption is
# raised after that, with the socket made closed and nothing left in the
# place for D. The thread is slowed down before it enters D, and what it
# enters is checked to be D, still there; the join raises once, then waits.
@pytest.mark.skipif(not thread_cwd_usable(), reason="the system does not give a thread a cwd of its own here")
def test_an_interrupted_wait_lets_the_thread_end_before_d_is_removed(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = far(tmp)
    d_place = tmp.new()
    made: list[socket.socket] = []
    entered: list[bool] = []
    alive_at_raise: list[bool] = []
    threads: list[threading.Thread] = []
    real_new_socket = _route.new_socket
    real_join = threading.Thread.join
    real_enter = _sys.enter_dir_in_this_thread

    def new_socket(req: _route.Request, *, standard: bool = False) -> socket.socket:
        sock = real_new_socket(req, standard=standard)
        made.append(sock)
        return sock

    def slow_enter(fd: int) -> bool:
        time.sleep(0.2)
        st = os.fstat(fd)
        entered.append(stat.S_ISDIR(st.st_mode) and len(listing(d_place)) == 1)
        return real_enter(fd)

    def join_interrupted_once(self: threading.Thread, timeout: float | None = None) -> None:
        if not threads:
            threads.append(self)
            raise KeyboardInterrupt
        real_join(self, timeout)

    monkeypatch.setattr(_route, "new_socket", new_socket)
    monkeypatch.setattr(_route, "enter_dir_in_this_thread", slow_enter)
    monkeypatch.setattr(threading.Thread, "join", join_interrupted_once)
    try:
        with pytest.raises(KeyboardInterrupt):
            _route.run_long(BIND, p, central_cfg((d_place,), proc=NO_PROC))
        alive_at_raise.append(threads[0].is_alive())
    finally:
        monkeypatch.setattr(threading.Thread, "join", real_join)
        for t in threads:
            t.join()
        for sock in made:
            sock.close()
    assert alive_at_raise == [False]
    assert entered == [True]
    assert made
    assert all(sock.fileno() == -1 for sock in made)
    assert listing(d_place) == []


# The pre-check of the thread's own cwd (made before D) agrees with the
# system: on macOS pthread_fchdir_np is there; on Linux it is the seccomp
# state (docker's default seccomp: a filter on, so not even tried).
def test_the_pre_check_of_the_threads_cwd_agrees_with_the_system() -> None:
    if sys.platform == "darwin":
        assert _sys.thread_cwd_possible()
    want = os.environ.get("SOCKAT_TEST_THREAD_CWD")
    if want is not None:
        assert _sys.thread_cwd_possible() is (want == "1")
    if thread_cwd_usable():
        assert _sys.thread_cwd_possible()


# ----- the bind is not widened -----


# On Apple the central route's spelling is within sun_path, so the address
# the listener's kernel side holds fits sun_path with its NUL, for every name
# length the route takes: no bind leaves a widened address.
@pytest.mark.skipif(not IS_APPLE, reason="the widened sockaddr is Apple's")
def test_an_apple_bind_through_the_central_route_leaves_an_address_within_sun_path(tmp: TmpDirs) -> None:
    t = tmp.new()
    for n in (6, 50, NAME_MAX_CENTRAL):
        p = nest(t, 600, b"a" * n)
        listener, r = _route.run_long(BIND, p, central_cfg())
        with listener:
            assert r is Route.THREAD_CWD
            assert len(os.fsencode(kernel_sockname(listener))) <= DIRECT_MAX
