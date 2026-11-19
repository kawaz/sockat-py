"""Helpers shared by the tests and by the child-process scenarios
(``_children.py``).

What every route test leans on:

* The socket must appear at the path the caller gave (checked with
  ``lstat``), not at whatever spelling was handed to the kernel. A route that
  "works" but creates the socket elsewhere is the failure these tests exist
  to catch.
* Temporary directories have random names, sit directly under ``/tmp`` (so
  the tests decide the path lengths: ``/tmp/sockat-XXXXXXXX`` is 20 bytes on
  every system) and are removed when the test ends (:class:`TmpDirs`).
* Anything that changes process-wide state (the cwd, ``$PWD``, ``$TMPDIR``)
  runs in a child process (:func:`run_child`), so the test process and the
  other tests never see it.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import functools
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import pytest

import sockat
from sockat import _route, _sys
from sockat._route import BIND, CONNECT, CONNECT_NONBLOCKING, SOCKET_DIR, Config, OwnDirPlace, Request, Route
from sockat._sockets import PathSocket
from sockat._sys import DIRECT_MAX, IS_APPLE, IS_LINUX

T = TypeVar("T")

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = Path(sockat.__file__).resolve().parents[1]
CHILDREN = TESTS_DIR / "_children.py"

PROC = b"/proc/self/fd"
"""Where the kernel lists the fds of the process (Linux)."""

NO_PROC = b"/nonexistent/proc/self/fd"
"""Where ``/proc/self/fd`` is said to be when /proc is to look absent
(Linux): a path that does not exist. Only the place to look is changed; the
real /proc is untouched."""

NO_PLACE = b"/nonexistent"
"""A place (for D or a symlink) that does not exist, so nothing can be made
there."""

FULL_BACKLOG = errno.EAGAIN if IS_LINUX else errno.ECONNREFUSED
"""What a nonblocking connect answers while the listener's backlog is full:
Linux ``EAGAIN`` (a blocking connect would wait for room instead), macOS
``ECONNREFUSED`` (as a blocking connect does)."""

CHECKS_FULL_BACKLOG = sys.platform.startswith("linux") or sys.platform == "darwin"
"""Whether :func:`check_full_backlog` applies here: what a nonblocking
connect answers on a full backlog is confirmed only on Linux and macOS (the
SPEC leaves the other systems open)."""

LOCAL_PEERPID = 0x002
"""``LOCAL_PEERPID`` of ``<sys/un.h>`` (Apple), at level ``SOL_LOCAL`` (0).
The ``socket`` module does not export either constant."""

SO_NOSIGPIPE = 0x1022
"""``SO_NOSIGPIPE`` of ``<sys/socket.h>`` (Apple). The ``socket`` module does
not export it."""

ASYNCIO_CLEANS_UP = sys.version_info >= (3, 13)
"""Whether ``asyncio.start_unix_server(sock=)`` cleans up the listener's
socket file (``cleanup_socket``, on by default, added in Python 3.13): at the
start it reads the listener's ``getsockname()`` and records the ``st_ino``
of ``os.stat`` of it (an error other than ``FileNotFoundError`` fails the
start), and when the server closes it reads ``getsockname()`` again and
removes the file there if ``os.stat`` still finds that inode. Before 3.13
the file is left where it is, as a plain ``close`` leaves it."""


# ---------------------------------------------------------------------------
# temporary directories and paths
# ---------------------------------------------------------------------------


class TmpDirs:
    """Randomly named directories directly under ``/tmp``, all removed by
    :meth:`cleanup` (whatever modes the test left inside them)."""

    def __init__(self) -> None:
        self._made: list[bytes] = []

    def new(self, under: str = "/tmp") -> bytes:
        """A new directory directly under ``under`` (``/tmp`` unless
        given)."""
        d = os.fsencode(tempfile.mkdtemp(prefix="sockat-", dir=under))
        self._made.append(d)
        return d

    def cleanup(self) -> None:
        for d in self._made:
            # A test may leave a directory without read permission (0300);
            # give every directory back 0700 so it can be listed and removed.
            for root, dirs, _ in os.walk(d):
                for name in dirs:
                    full = os.path.join(root, name)
                    if not os.path.islink(full):
                        os.chmod(full, 0o700)
            if os.path.isdir(d):
                os.chmod(d, 0o700)
            shutil.rmtree(d, ignore_errors=True)


def nest(base: bytes, total: int, name: bytes = b"s.sock") -> bytes:
    """Create directories under ``base`` so that ``<base>/<dirs>/<name>`` is
    exactly ``total`` bytes long, and return that socket path. Directory names
    are runs of ``d`` of at most 60 bytes (well under ``NAME_MAX``)."""
    fixed = len(base) + 1 + len(name)
    assert total >= fixed, f"{total} < {fixed}"
    remaining = total - fixed
    d = base
    while remaining > 0:
        # Each directory adds "<k bytes>/", so a remainder of 1 cannot be
        # made; avoid leaving one behind.
        assert remaining >= 2, "cannot pad by exactly one byte"
        k = min(remaining - 1, 60)
        if remaining - (k + 1) == 1:
            k -= 1
        d = d + b"/" + b"d" * k
        remaining -= k + 1
    os.makedirs(d, exist_ok=True)
    p = d + b"/" + name
    assert len(p) == total
    return p


def nest_tail(directory: bytes, tail: int, name: bytes = b"s.sock") -> bytes:
    """:func:`nest` with the length given for the part below ``directory``:
    the socket path is ``<directory>/<tail bytes>``."""
    return nest(directory, len(directory) + 1 + tail, name)


def parent(path: bytes) -> bytes:
    return os.path.dirname(path)


def with_name(path: bytes, name: bytes) -> bytes:
    return os.path.join(os.path.dirname(path), name)


def is_socket(p: bytes) -> bool:
    try:
        return stat.S_ISSOCK(os.lstat(p).st_mode)
    except FileNotFoundError:
        return False


def listing(d: bytes) -> list[bytes]:
    return sorted(os.listdir(d))


def place(parent_dir: bytes, name: bytes, mode: int) -> bytes:
    """A directory ``name`` under ``parent_dir`` with exactly ``mode`` (set
    after creating it, so the umask does not interfere)."""
    d = parent_dir + b"/" + name
    os.mkdir(d)
    os.chmod(d, mode)
    return d


def assert_empty(d: bytes) -> None:
    """Nothing was left in ``d`` (the place for the symlink)."""
    left = os.listdir(d)
    assert left == [], f"left behind in {d!r}: {left!r}"


UNTOUCHED = 0
"""The modification time :func:`mark_untouched` gives a directory (the
epoch)."""


def mark_untouched(d: bytes) -> None:
    """Set the modification time of ``d`` to a fixed time in the past, so a
    later :func:`assert_untouched` can tell whether anything was created in
    it or removed from it meanwhile (either updates a directory's mtime).
    :func:`assert_empty` alone cannot: a symlink made and removed again leaves
    the directory empty."""
    os.utime(d, (UNTOUCHED, UNTOUCHED))
    assert_untouched(d)


def assert_untouched(d: bytes) -> None:
    assert os.stat(d).st_mtime == UNTOUCHED, f"something was created in or removed from {d!r}"


def dir_id(d: bytes | str) -> tuple[int, int]:
    st = os.stat(d)
    return st.st_dev, st.st_ino


# ---------------------------------------------------------------------------
# sockets
# ---------------------------------------------------------------------------


def recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        assert chunk, "the peer closed the connection"
        buf += chunk
    return buf


def ping(server: socket.socket, client: socket.socket) -> None:
    """Exchange a message in each direction between an accepted stream and
    its client. A Unix stream write lands in the peer's receive queue before
    it returns, so this works on a nonblocking client too."""
    client.sendall(b"ping")
    assert recv_exact(server, 4) == b"ping"
    server.sendall(b"pong")
    assert recv_exact(client, 4) == b"pong"


def accept_ping(listener: socket.socket, client: socket.socket) -> None:
    """Accept ``client`` on ``listener`` and :func:`ping` over it."""
    server, _ = listener.accept()
    with server:
        ping(server, client)


def kernel_sockname(s: socket.socket) -> str | bytes:
    """The address the kernel reports for ``s`` itself: the standard
    ``getsockname``, past what :class:`PathSocket` reports in its place (for
    a listener bound through a route, the spelling the route handed to the
    kernel)."""
    addr = socket.socket.getsockname(s)
    assert isinstance(addr, (str, bytes)), addr
    return addr


def kernel_peername(s: socket.socket) -> str | bytes:
    """The address the kernel reports for the peer of ``s``: the standard
    ``getpeername``, past what :class:`PathSocket` reports in its place."""
    addr = socket.socket.getpeername(s)
    assert isinstance(addr, (str, bytes)), addr
    return addr


def assert_sockat_type(what: str, s: socket.socket, path: str | bytes) -> None:
    """``s`` is of the type sockat returns for ``path`` (as given, so a
    relative path counts by its own length): a plain :class:`socket.socket`,
    nothing added, for a path the ``socket`` module takes (at most
    :data:`DIRECT_MAX` bytes), and for a longer one a :class:`PathSocket`, a
    subclass of it that reports the path as the address (the kernel holds a
    route's spelling there instead)."""
    want = socket.socket if len(os.fsencode(path)) <= DIRECT_MAX else PathSocket
    assert type(s) is want, f"{what}: {type(s)}, not {want}"


def is_cloexec(s: socket.socket | int) -> bool:
    fd = s if isinstance(s, int) else s.fileno()
    return bool(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)


def peer_pid(server: socket.socket) -> int | None:
    """The pid the server's peer-credential query names for ``server`` (an
    accepted stream): ``SO_PEERCRED`` on Linux, ``LOCAL_PEERPID`` on Apple;
    ``None`` on the systems where the query is not checked (the SPEC leaves
    the peer credentials of NetBSD, OpenBSD and illumos unconfirmed)."""
    if sys.platform == "linux":
        pid, _, _ = struct.unpack("3i", server.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return int(pid)
    if sys.platform == "darwin":
        return server.getsockopt(0, LOCAL_PEERPID)
    return None


@contextlib.contextmanager
def raises_errno(code: int) -> Iterator[None]:
    """The block must raise an :class:`OSError` with errno ``code`` (for the
    errnos Python has no subclass for, such as ``EADDRINUSE`` and
    ``ENAMETOOLONG``)."""
    try:
        yield
    except OSError as e:
        if e.errno != code:
            raise AssertionError(f"{e!r}, not {errno.errorcode[code]}") from e
    else:
        raise AssertionError(f"no error, not {errno.errorcode[code]}")


def at_once(f: Callable[[], T]) -> T:
    """Run ``f`` on another thread and return its result (or raise its
    exception), failing the test if it has not returned within 10 seconds.

    The calls run through this must return at once (a nonblocking connect to
    a full backlog). Should one block instead, as a blocking connect on Linux
    does until the listener accepts, the test fails here rather than hanging.
    The 10 seconds only stand for "never returns": a call that returns does
    so within microseconds, so it is no estimate of a duration.
    """
    box: list[T] = []
    failure: list[BaseException] = []

    def target() -> None:
        try:
            box.append(f())
        except BaseException as e:
            failure.append(e)

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(10)
    assert not th.is_alive(), "the call blocked"
    if failure:
        raise failure[0]
    return box[0]


def listener_at(path: bytes) -> socket.socket:
    """A listener on a socket file at ``path``, bound by the standard under a
    short name in a fresh directory directly under ``/tmp`` and renamed to
    ``path`` (which must be on the same file system, as every test path under
    ``/tmp`` is). For paths no bind route reaches (a file name too long for
    every sockaddr), and for listeners that must not depend on the routes
    under test: the socket file is the same inode after the rename, so the
    listener accepts connections made to ``path``."""
    short_dir = tempfile.mkdtemp(prefix="sockat-l-", dir="/tmp")
    short = os.path.join(short_dir, "l.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(short)
        listener.listen()
        os.rename(os.fsencode(short), path)
    except BaseException:
        listener.close()
        raise
    finally:
        with contextlib.suppress(OSError):
            os.unlink(short)
        os.rmdir(short_dir)
    return listener


# ---------------------------------------------------------------------------
# configurations
# ---------------------------------------------------------------------------


def fallback_cfg(
    links: Sequence[bytes] = (), *, wide: bool = IS_APPLE, proc: bytes = NO_PROC, thread_cwd: bool = False
) -> Config:
    """The route order's inputs with no place for D and, unless turned on,
    neither the thread's own cwd nor /proc (so neither the routes through D nor
    the form without D can be used, bindat on FreeBSD aside, and a bind ends
    at the fork route's place, where this implementation answers
    ``ENAMETOOLONG``), ``links`` as the places for a connect's symlink, and
    on Linux /proc made to look absent unless ``proc`` says where to look.
    ``wide`` off stands in for an Apple kernel that refuses the widened
    sockaddr."""
    return Config(own_dir_places=(), link_places=tuple(links), wide=wide, proc_self_fd=proc, thread_cwd=thread_cwd)


def d_cfg(
    own: Sequence[OwnDirPlace] = (SOCKET_DIR,),
    links: Sequence[bytes] = (),
    *,
    thread_cwd: bool = True,
    proc: bytes = PROC,
    wide: bool = IS_APPLE,
    connect_order: tuple[Route, ...] | None = None,
    bind_symlink_file: bool = not IS_LINUX,
) -> Config:
    """The route order's inputs with ``own`` as the places for D (the socket's
    directory unless given), the thread's own cwd tried unless turned off,
    the real /proc unless ``proc`` says where else to look, ``links`` as the
    places for symlink-file (no place unless given, so a bind's symlink-file
    cannot be used), the connect order of this system unless
    ``connect_order`` forces another, and a bind's symlink-file tried where
    the system has it unless ``bind_symlink_file`` says otherwise."""
    return Config(
        own_dir_places=tuple(own),
        link_places=tuple(links),
        wide=wide,
        proc_self_fd=proc,
        thread_cwd=thread_cwd,
        connect_order=connect_order,
        bind_symlink_file=bind_symlink_file,
    )


PROC_ORDER_THROUGH_D = (Route.THREAD_CWD, Route.PROC, Route.PROC_HELD, Route.SYMLINK_FILE)
"""The connect order where /proc can be used (Linux) without proc-direct, its
first route: forces a connect through the routes through D's /proc means
(the tests force each route, SPEC). proc-direct's spelling is the /proc
means' less ``R/`` (11 bytes), so it carries the file names the /proc means
carries, and a connect in the order of the SPEC reaches the /proc means only
where the socket's directory cannot be opened."""

FREEBSD_ORDER_THROUGH_D = (Route.AT,)
"""FreeBSD's connect order without at-direct, its first route: forces a
connect through ``connectat`` on D (at-direct otherwise carries every file
name at carries)."""


@functools.cache
def thread_cwd_usable() -> bool:
    """Whether a throwaway thread gets a cwd of its own here (Linux:
    ``unshare(CLONE_FS)``, not tried under a seccomp filter such as docker's
    default; Apple: ``pthread_fchdir_np``).

    Probed on the running system. Where the answer is known in advance it is
    asserted, so a broken probe cannot quietly move the tests to the "not
    usable" side: on macOS ``pthread_fchdir_np`` is there, and the docker
    recipes set ``SOCKAT_TEST_THREAD_CWD`` to ``1`` (``seccomp=unconfined``)
    or ``0`` (the default seccomp)."""
    if not _sys.HAS_THREAD_CWD:
        return False
    fd = os.open(b"/", os.O_RDONLY)
    got: list[bool] = []

    def probe() -> None:
        got.append(_sys.enter_dir_in_this_thread(fd))

    try:
        th = threading.Thread(target=probe)
        th.start()
        th.join()
    finally:
        os.close(fd)
    usable = got[0]
    assert usable == _sys.thread_cwd_possible() or IS_LINUX, "the pre-check disagrees with the thread"
    want = os.environ.get("SOCKAT_TEST_THREAD_CWD")
    if want is not None:
        assert usable == (want == "1"), f"SOCKAT_TEST_THREAD_CWD={want} but the probe says {usable}"
    if sys.platform == "darwin":
        assert usable, "pthread_fchdir_np is expected on macOS"
    return usable


HAS_PROC = IS_LINUX and os.path.isdir(b"/proc/self/fd")
"""Whether /proc can be used here (Linux)."""


def d_route_here() -> Route | None:
    """The routes through D the real configuration takes here for a short file
    name, or ``None`` where it has no means (NetBSD, OpenBSD, illumos): a
    thread's own cwd where it can be used, else bindat on FreeBSD, else
    /proc on Linux."""
    if sys.platform.startswith("freebsd"):
        return Route.AT
    if thread_cwd_usable():
        return Route.THREAD_CWD
    if HAS_PROC:
        return Route.PROC
    return None


@functools.cache
def bind_follows_a_symlink() -> bool:
    """Whether a bind here follows a symlink in the last component to a path
    that does not exist yet, making the socket there (what a bind's
    symlink-file needs). Probed with the ``socket`` module in a fresh
    directory under ``/tmp``. Where the answer is known it is asserted, so a
    broken probe cannot quietly move the tests to the other side: macOS
    follows, Linux does not (measured, hub DR-0016)."""
    d = tempfile.mkdtemp(prefix="sockat-f-", dir="/tmp")
    try:
        target = os.path.join(d, "s.sock")
        link = os.path.join(d, "l")
        os.symlink(target, link)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s, contextlib.suppress(OSError):
            s.bind(link)
        follows = is_socket(os.fsencode(target))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if sys.platform == "darwin":
        assert follows, "a bind is expected to follow a symlink on macOS"
    if IS_LINUX:
        assert not follows, "a bind is expected not to follow a symlink on Linux"
    return follows


def bind_route_here() -> Route | None:
    """The route the default bind takes here, with the configuration read
    from the environment, for a long path with a short file name: symlink-file
    where a bind tries it (every system but Linux) and follows the link,
    else the routes through D (:func:`d_route_here`)."""
    if not IS_LINUX and bind_follows_a_symlink():
        return Route.SYMLINK_FILE
    return d_route_here()


def skipped_before_d_here() -> list[str]:
    """What the default bind skips before the routes through D here, with a
    usable place for the symlink: the standard (the path is too long), and,
    where a bind tries symlink-file but does not follow it, symlink-file."""
    if not IS_LINUX and not bind_follows_a_symlink():
        return ["standard", "symlink-file"]
    return ["standard"]


def far_connect_here() -> Route | None:
    """The route the connect order of this system takes, with the real
    configuration, for a path far from the cwd and longer than any sockaddr
    carries (600 bytes, so neither relative nor widened), with a short file
    name: proc-direct where /proc can be used, at-direct on FreeBSD, the
    thread's own cwd where it can be used, else ``None`` (the symlink, when a
    place for it is given)."""
    if sys.platform.startswith("freebsd"):
        return Route.AT_DIRECT
    if HAS_PROC:
        return Route.PROC_DIRECT
    if thread_cwd_usable():
        return Route.THREAD_CWD
    return None


needs_long_bind = pytest.mark.skipif(
    bind_route_here() is None,
    reason="no route of the default bind here (no symlink-file a bind follows, no means of the routes through D)",
)
"""For the tests that bind long paths through the public API."""

NAME_MAX_THROUGH_D = DIRECT_MAX - _route.IN_OWN_DIR_LEN
"""The longest file name the routes through D bind by a thread's own cwd or
bindat (``R/<name>`` in a sockaddr): 96 on Linux, 92 on Apple and the
BSDs."""


def routes(bind: Route, connect: Route) -> Callable[[Request], Route]:
    """The route a request takes where a bind and a connect go different
    ways: ``bind`` for the bind, ``connect`` for both connects."""

    def want(req: Request) -> Route:
        return bind if req.op is _sys.Op.BIND else connect

    return want


def assert_far_from_cwd(path: bytes) -> None:
    """The premise of a test that forces a route after the relative one: the
    path has no relative spelling that fits the plain sockaddr from the cwd
    of the test process (the cwd is the project root, far from ``/tmp``)."""
    directory, _ = os.path.split(path)
    rel = _route.Relative.new(path, directory)
    assert rel is None or len(rel.path) > DIRECT_MAX, f"the premise: {path!r} is far from the cwd"


# ---------------------------------------------------------------------------
# one route, forced
# ---------------------------------------------------------------------------

Way = Callable[[Request], tuple[socket.socket, Route]]


@dataclass(frozen=True)
class Forced:
    """One socket path reached one fixed way: the route order with its inputs
    chosen so that one route is taken, and the route each request must take
    there."""

    what: str
    path: bytes
    way: Way
    want: Callable[[Request], Route]


def forced(
    what: str,
    path: bytes,
    cfg: Config | None,
    want: Route | Callable[[Request], Route],
    *,
    bind_cfg: Config | None = None,
) -> Forced:
    """A :class:`Forced` through :func:`_route.run_long` with ``cfg``, or
    through :func:`_route.run` (as the public API goes) when ``cfg`` is
    ``None``; the bind with ``bind_cfg`` when given (for a connect's
    fallback, which no bind takes)."""

    def way(req: Request) -> tuple[socket.socket, Route]:
        if req.op is _sys.Op.BIND and bind_cfg is not None:
            return _route.run_long(req, path, bind_cfg)
        if cfg is None:
            return _route.run(req, path)
        return _route.run_long(req, path, cfg)

    def want_fn(req: Request) -> Route:
        return want if isinstance(want, Route) else want(req)

    return Forced(what, path, way, want_fn)


def check_route(f: Forced, places: Sequence[bytes] = ()) -> None:
    """Check everything one route must do, through ``f``:

    * the bind takes the route, the socket file is at the path the caller
      gave, and the listener is a blocking ``socket.socket`` like the
      standard's;
    * ``connect`` and ``connect_nonblocking`` take the route (the same one:
      no route differs by blocking) and return a blocking / nonblocking
      socket; a read from the nonblocking one with nothing to read answers
      ``BlockingIOError`` instead of waiting;
    * data flows both ways between the listener and each client;
    * every socket is close-on-exec, as the ``socket`` module makes its own;
    * the server's peer-credential query names this process (no route
      connects from another process), on Linux and Apple;
    * every socket is of the type sockat returns for the path
      (:func:`assert_sockat_type`: the standard's for a path it takes, a
      :class:`PathSocket` past that), and the addresses are what the
      standard reports for a path it binds: the path (a ``str``, decoded by
      :func:`os.fsdecode`) as the listener's and the accepted stream's own
      address and as the client's peer, whatever spelling the route handed
      to the kernel; the client's own address and the accepted stream's peer
      are the kernel's (a client is not bound);
    * the addresses the kernel itself holds (:func:`kernel_sockname` /
      :func:`kernel_peername` of the listener, the client and the accepted
      stream) can be read without an error, and each fits ``sun_path`` with
      its NUL (:func:`assert_address_fits`);
    * the process cwd is the same afterwards, nothing but the socket file is
      in its directory (no D or R named ``.sockat.*``), and nothing is left
      in ``places`` (the places for D or the symlink);
    * the listener and both connects, handed to asyncio, serve, report the
      path, and are cleaned up as asyncio cleans up the standard's
      (:func:`check_asyncio`).

    The socket file is removed at the end, so the path can be used again.
    """
    cwd, cwd_id = os.getcwd(), dir_id(".")
    me = os.getpid()
    name = os.fsdecode(f.path)
    listener, r = f.way(BIND)
    with listener:
        assert r is f.want(BIND), f"{f.what}: bind took {r}"
        assert is_socket(f.path), f"{f.what}: no socket at the path"
        assert_sockat_type(f"{f.what}: the listener", listener, f.path)
        assert listener.getblocking(), f"{f.what}: the listener is blocking, as the standard's"
        assert is_cloexec(listener), f"{f.what}: listener"
        assert listener.getsockname() == name, f"{f.what}: the listener"
        assert_address_fits(f"{f.what}: the listener", kernel_sockname(listener))
        for req, blocking in ((CONNECT, True), (CONNECT_NONBLOCKING, False)):
            client, r = f.way(req)
            with client:
                assert r is f.want(req), f"{f.what}: {req} took {r}"
                assert_sockat_type(f"{f.what}: {req}", client, f.path)
                assert client.getblocking() is blocking, f"{f.what}: {req}"
                assert is_cloexec(client), f"{f.what}: {req}"
                assert client.getpeername() == name, f"{f.what}: {req} client's peer"
                assert client.getsockname() == kernel_sockname(client), f"{f.what}: {req} client"
                assert_address_fits(f"{f.what}: {req} client", kernel_sockname(client))
                assert_address_fits(f"{f.what}: {req} client's peer", kernel_peername(client))
                server, _ = listener.accept()
                with server:
                    assert_sockat_type(f"{f.what}: {req} server", server, f.path)
                    assert server.getsockname() == name, f"{f.what}: {req} server"
                    assert server.getpeername() == kernel_peername(server), f"{f.what}: {req} server's peer"
                    assert_address_fits(f"{f.what}: {req} server", kernel_sockname(server))
                    assert_address_fits(f"{f.what}: {req} server's peer", kernel_peername(server))
                    assert peer_pid(server) in (me, None), f"{f.what}: {req}"
                    if not blocking:
                        try:
                            client.recv(1)
                        except BlockingIOError:
                            pass
                        else:
                            raise AssertionError(f"{f.what}: a read with nothing to read did not answer EAGAIN")
                    ping(server, client)
    assert os.getcwd() == cwd, f"{f.what}: the cwd changed"
    assert dir_id(".") == cwd_id, f"{f.what}: the cwd changed"
    assert listing(parent(f.path)) == [os.path.basename(f.path)], f"{f.what}: left next to the socket"
    for p in places:
        assert_empty(p)
    os.unlink(f.path)
    check_asyncio(f)
    for p in places:
        assert_empty(p)


def assert_address_fits(what: str, addr: str | bytes) -> None:
    """An address the kernel holds for a socket (:func:`kernel_sockname` /
    :func:`kernel_peername`) is at most :data:`DIRECT_MAX` bytes: what the
    route bound fits ``sun_path`` with its NUL.

    Why it must, although a :class:`PathSocket` reports the path in its
    place: the ``socket`` module of CPython reads a pathname address on the
    systems other than Linux up to the first NUL in ``sun_path``, not by the
    length the kernel returns. An address that fills ``sun_path`` with no NUL
    (a bind in the widened sockaddr on Apple) is read past the end of
    ``sun_path`` into memory it never cleared, so it comes back as its first
    104 bytes followed by whatever bytes lie there, different from run to
    run. Whoever reads the kernel's address (the standard ``getsockname`` /
    ``getpeername``, which :class:`PathSocket` calls first so that it fails
    where the standard's fails; a client in another process, whose peer is
    this listener) reads memory past the address. So no bind route may leave
    an address longer than this, and a bind in the widened form always does
    (its path is over :data:`DIRECT_MAX` bytes, and the module reports at
    least the 104 bytes of ``sun_path``), which makes the check deterministic
    rather than a matter of luck. An unbound socket (a client) reports the
    empty address."""
    shown = os.fsencode(addr)
    assert len(shown) <= DIRECT_MAX, f"{what}: an address of {len(shown)} bytes: {shown!r}"


Connect = Callable[[Request], socket.socket]


async def serve_asyncio(what: str, listener: socket.socket, connect: Connect, path: str, served: bool) -> None:
    """Hand ``listener`` to asyncio (``asyncio.start_unix_server(sock=)``,
    with nothing else given, so ``cleanup_socket`` is left at its default)
    and adopt a nonblocking and a blocking connect made by ``connect``
    (``asyncio.open_unix_connection(sock=)``), exchange data both ways, and
    close the server.

    The client's transport reports ``path`` as its ``peername``. When
    ``served`` (the listener was bound at ``path``, rather than renamed
    there), the server's socket reports ``path`` as its address, and so does
    the transport of every stream it accepted (``sockname``), as asyncio
    reports the standard's."""
    names: list[object] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        names.append(writer.get_extra_info("sockname"))
        data = await reader.readexactly(4)
        writer.write(b"pong" if data == b"ping" else b"????")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, sock=listener)
    async with server:
        if served:
            assert [s.getsockname() for s in server.sockets] == [path], what
        for req in (CONNECT_NONBLOCKING, CONNECT):
            reader, writer = await asyncio.open_unix_connection(sock=connect(req))
            assert writer.get_extra_info("peername") == path, f"{what}: {req}"
            writer.write(b"ping")
            await writer.drain()
            assert await reader.readexactly(4) == b"pong", f"{what}: {req}"
            writer.close()
            await writer.wait_closed()
    assert len(names) == 2, what
    if served:
        assert names == [path, path], what


def assert_cleaned_up_as_asyncio_does(what: str, path: bytes) -> None:
    """After a server that served a listener bound at ``path`` closed, the
    socket file is where asyncio leaves the standard's
    (:data:`ASYNCIO_CLEANS_UP`): removed from Python 3.13, still there
    before (and removed here, so the path can be used again)."""
    if ASYNCIO_CLEANS_UP:
        assert not os.path.lexists(path), f"{what}: asyncio did not remove the socket file at the path"
    else:
        assert is_socket(path), f"{what}: the socket file is not where asyncio leaves it"
        os.unlink(path)


def check_asyncio(f: Forced) -> None:
    """Use the sockets of ``f`` with asyncio the way the standard's are used,
    with nothing added (:func:`serve_asyncio`), twice:

    * bound before any event loop exists (``asyncio.run`` after the bind).
      This is how a listener is usually made, and the one that matters on
      Linux: the routes through D through /proc binds
      ``/proc/self/fd/<n>/R/<name>`` with D's fd, closed again, and the event
      loop made afterwards takes that free fd number for its own (the
      selector, the self-pipe), so the kernel's spelling then names something
      else (a bind through /proc without R gave ``NotADirectoryError`` from
      ``os.stat`` at the start of the server, had asyncio been handed it);
    * bound on the loop's thread with the loop running.

    Every step must go through without an error, on every route: the start
    of the server (Python 3.13 and later ``os.stat`` the listener's
    address), the exchange, the close (which, from 3.13, removes the file at
    the listener's address). Afterwards the socket file at the path is
    removed from 3.13 and left before, as for the standard's
    (:func:`assert_cleaned_up_as_asyncio_does`): asyncio reads the path,
    not a spelling that might name another file or none."""
    path = os.fsdecode(f.path)

    def bind() -> socket.socket:
        listener, r = f.way(BIND)
        assert r is f.want(BIND), f"{f.what}: bind took {r}"
        return listener

    def connect(req: Request) -> socket.socket:
        sock, r = f.way(req)
        assert r is f.want(req), f"{f.what}: {req} took {r}"
        return sock

    what = f"{f.what}, bound before the loop"
    listener = bind()
    asyncio.run(serve_asyncio(what, listener, connect, path, served=True))
    assert_cleaned_up_as_asyncio_does(what, f.path)

    async def bound_in_the_loop() -> None:
        await serve_asyncio(f"{f.what}, bound in the loop", bind(), connect, path, served=True)

    asyncio.run(bound_in_the_loop())
    assert_cleaned_up_as_asyncio_does(f"{f.what}, bound in the loop", f.path)


def check_full_backlog(f: Forced) -> None:
    """Check the nonblocking connect of ``f`` against a full backlog: with
    the listener's backlog shrunk to 1 and filled, a nonblocking connect
    fails at once with :data:`FULL_BACKLOG` within a few attempts (Linux lets
    backlog + 1 connections wait, macOS 1), on the same route, and keeps
    failing that way while it stays full. Every connect that got in
    meanwhile is nonblocking too. The socket file is removed at the end."""
    listener, r = f.way(BIND)
    with listener:
        assert r is f.want(BIND)
        listener.listen(1)
        queued: list[socket.socket] = []
        try:
            while True:
                assert len(queued) < 16, f"{f.what}: the backlog never filled"
                try:
                    c, r = at_once(lambda: f.way(CONNECT_NONBLOCKING))
                except OSError as e:
                    full = e.errno
                    break
                queued.append(c)
                assert r is f.want(CONNECT_NONBLOCKING), f"{f.what}: {r}"
                assert not c.getblocking(), f"{f.what}"
            assert queued, f"{f.what}: nothing got in before it was full"
            assert full == FULL_BACKLOG, f"{f.what}: {errno.errorcode.get(full or 0)}"
            with raises_errno(FULL_BACKLOG):
                at_once(lambda: f.way(CONNECT_NONBLOCKING))[0].close()
        finally:
            for c in queued:
                c.close()
    os.unlink(f.path)


def check_unreachable(path: bytes, cfg: Config, untouched: Sequence[bytes] = ()) -> None:
    """Check that a path no route reaches with ``cfg`` is ``ENAMETOOLONG``
    for the bind and both connects (this implementation has no fork route),
    and that nothing happens: nothing appears next to the socket, no
    connection reaches a listener at the path, and nothing is created in the
    directories ``untouched`` (marked by the caller with
    :func:`mark_untouched`)."""
    directory = parent(path)
    had_socket = is_socket(path)
    listener = listener_at(path) if not had_socket else None
    try:
        before = listing(directory)
        if listener is not None:
            listener.setblocking(False)
        for req in (BIND, CONNECT, CONNECT_NONBLOCKING):
            with raises_errno(errno.ENAMETOOLONG):
                _route.run_long(req, path, cfg)[0].close()
            if listener is not None:
                try:
                    listener.accept()[0].close()
                except BlockingIOError:
                    pass
                else:
                    raise AssertionError(f"{req}: a connection got in")
        assert listing(directory) == before
        for d in untouched:
            assert_untouched(d)
    finally:
        if listener is not None:
            listener.close()


# ---------------------------------------------------------------------------
# child processes
# ---------------------------------------------------------------------------


def run_child(
    scenario: str,
    cwd: bytes,
    args: Mapping[str, bytes] | None = None,
    env: Mapping[str, bytes] | None = None,
) -> dict[str, str]:
    """Run ``scenario`` of ``_children.py`` in a child process (this Python,
    ``sys.executable``) whose cwd is ``cwd`` and ``$PWD`` is ``cwd`` unless
    ``env`` gives another, require it to succeed, and return what it
    reported. ``args`` reach the child as ``SOCKAT_ARG_<KEY>``."""
    child_env: dict[bytes, bytes] = dict(os.environb)
    # The source and the tests first, then whatever the test process had
    # (where pytest itself was installed, which _support imports).
    paths = [os.fsencode(SRC_DIR), os.fsencode(TESTS_DIR)]
    if os.environb.get(b"PYTHONPATH"):
        paths.append(os.environb[b"PYTHONPATH"])
    child_env[b"PYTHONPATH"] = os.fsencode(os.pathsep).join(paths)
    child_env[b"PWD"] = cwd
    for k, v in (env or {}).items():
        child_env[os.fsencode(k)] = v
    for k, v in (args or {}).items():
        child_env[b"SOCKAT_ARG_" + os.fsencode(k)] = v
    out = subprocess.run(
        [sys.executable, str(CHILDREN), scenario],
        cwd=cwd,
        env=child_env,
        capture_output=True,
        check=False,
    )
    stdout = out.stdout.decode(errors="replace")
    stderr = out.stderr.decode(errors="replace")
    assert out.returncode == 0, f"child {scenario} failed\n--- stdout\n{stdout}\n--- stderr\n{stderr}"
    for line in stdout.splitlines():
        if line.startswith(RESULT_MARK):
            parsed = json.loads(line[len(RESULT_MARK) :])
            assert isinstance(parsed, dict)
            return {str(k): str(v) for k, v in parsed.items()}
    raise AssertionError(f"child {scenario} reported nothing\n{stdout}\n{stderr}")


RESULT_MARK = "sockat-child: "


def stale_socket(path: bytes) -> None:
    """Leave a socket file at ``path`` that no process listens on, made by a
    child process that binds and exits (the SPEC asks for another process: a
    listener closed in this one could still be held open by a copy of the fd
    in some other child of this process)."""
    run_child("make_stale_socket", b"/", {"PATH": path})
    assert is_socket(path)
