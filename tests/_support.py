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

import sockat
from sockat import _route, _sys
from sockat._route import BIND, CONNECT, CONNECT_NONBLOCKING, Config, Request, Route
from sockat._sys import DIRECT_MAX, IS_APPLE, IS_LINUX

T = TypeVar("T")

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = Path(sockat.__file__).resolve().parents[1]
CHILDREN = TESTS_DIR / "_children.py"

NO_PROC = b"/nonexistent/proc/self/fd"
"""Where ``/proc/self/fd`` is said to be when /proc is to look absent
(Linux): a path that does not exist. Only the place to look is changed; the
real /proc is untouched."""

NO_PLACE = b"/nonexistent"
"""A place for the symlink that does not exist (so the symlink route cannot
be used there)."""

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
    """A listener on a socket file at ``path``, bound under a short name in
    the same directory through the public API and renamed to ``path``. For
    paths no bind route reaches (a file name too long for every sockaddr):
    the socket file is the same inode after the rename, so the listener
    accepts connections made to ``path``."""
    short = with_name(path, b"l.sock")
    listener = sockat.bind(short)
    os.rename(short, path)
    return listener


# ---------------------------------------------------------------------------
# configurations
# ---------------------------------------------------------------------------


def posix_cfg(
    bases: Sequence[bytes] = (), *, wide: bool = IS_APPLE, thread_cwd: bool = False, proc: bytes = NO_PROC
) -> Config:
    """The route order's inputs with ``bases`` as the places for the symlink,
    and on Linux /proc made to look absent, so the POSIX order runs there
    too. ``wide`` off stands in for an Apple kernel that refuses the widened
    sockaddr. The thread-cwd route is off unless asked for, so a path that
    falls through every other route reaches the fork route's place, where
    this implementation answers ``ENAMETOOLONG``. ``proc`` is where
    ``/proc/self/fd`` is said to be."""
    return Config(tmp_bases=tuple(bases), wide=wide, proc_self_fd=proc, thread_cwd=thread_cwd)


@functools.cache
def thread_cwd_usable() -> bool:
    """Whether the thread-cwd route can be used here: a throwaway thread gets
    a cwd of its own (Linux: ``unshare(CLONE_FS)``, which docker's default
    seccomp refuses with ``EPERM``; Apple: ``pthread_fchdir_np``).

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
        entered = _sys.enter_dir_in_this_thread(fd)
        if entered:
            _sys.leave_dir_in_this_thread()
        got.append(entered)

    try:
        th = threading.Thread(target=probe)
        th.start()
        th.join()
    finally:
        os.close(fd)
    usable = got[0]
    want = os.environ.get("SOCKAT_TEST_THREAD_CWD")
    if want is not None:
        assert usable == (want == "1"), f"SOCKAT_TEST_THREAD_CWD={want} but the probe says {usable}"
    if sys.platform == "darwin":
        assert usable, "pthread_fchdir_np is expected on macOS"
    return usable


def symlink_route(req: Request) -> Route:
    """The symlink route a request takes: a bind goes through a link to the
    socket's directory, a connect through a link to the socket file."""
    return Route.SYMLINK_TO_DIR if req.op is _sys.Op.BIND else Route.SYMLINK_TO_FILE


def symlink_wide_route(req: Request) -> Route:
    """:func:`symlink_route` in the widened sockaddr (Apple)."""
    return Route.SYMLINK_TO_DIR_WIDE if req.op is _sys.Op.BIND else Route.SYMLINK_TO_FILE_WIDE


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


def forced(what: str, path: bytes, cfg: Config | None, want: Route | Callable[[Request], Route]) -> Forced:
    """A :class:`Forced` through :func:`_route.run_long` with ``cfg``, or
    through :func:`_route.run` (as the public API goes) when ``cfg`` is
    ``None``."""

    def way(req: Request) -> tuple[socket.socket, Route]:
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
    * the addresses the kernel reports (``getsockname`` / ``getpeername`` of
      the listener, the client and the accepted stream) can be read without
      an error, whatever spelling the route bound;
    * the process cwd is the same afterwards, and nothing is left in
      ``places`` (the places for the symlink).

    The socket file is removed at the end, so the path can be used again.
    """
    cwd, cwd_id = os.getcwd(), dir_id(".")
    me = os.getpid()
    listener, r = f.way(BIND)
    with listener:
        assert r is f.want(BIND), f"{f.what}: bind took {r}"
        assert is_socket(f.path), f"{f.what}: no socket at the path"
        assert isinstance(listener, socket.socket)
        assert listener.getblocking(), f"{f.what}: the listener is blocking, as the standard's"
        assert is_cloexec(listener), f"{f.what}: listener"
        listener.getsockname()
        for req, blocking in ((CONNECT, True), (CONNECT_NONBLOCKING, False)):
            client, r = f.way(req)
            with client:
                assert r is f.want(req), f"{f.what}: {req} took {r}"
                assert client.getblocking() is blocking, f"{f.what}: {req}"
                assert is_cloexec(client), f"{f.what}: {req}"
                client.getsockname()
                client.getpeername()
                server, _ = listener.accept()
                with server:
                    server.getsockname()
                    server.getpeername()
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
    for p in places:
        assert_empty(p)
    os.unlink(f.path)


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
    """Check that a path only the fork route would reach is ``ENAMETOOLONG``
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
    child_env[b"PYTHONPATH"] = os.fsencode(f"{SRC_DIR}{os.pathsep}{TESTS_DIR}")
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
