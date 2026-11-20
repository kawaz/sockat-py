"""Helpers shared by the tests.

What every route test leans on:

* The socket must appear at the path the caller gave (checked with
  ``lstat``), not at whatever spelling was handed to the kernel. A route that
  "works" but creates the socket elsewhere is the failure these tests exist
  to catch.
* Temporary directories have random names, sit directly under ``/tmp`` (so
  the tests decide the path lengths) and are removed when the test ends
  (:class:`TmpDirs`).
* The places for B are directories of the test (:func:`env_at`), never the
  real ``/tmp/sockat-<uid>``, except in the few tests of the default places,
  which run in a child process and remove what it made.
* Anything that changes process-wide state (the cwd, the environment) runs
  in a child process (:func:`run_child`).
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

import pytest

import sockat
from sockat import _calls, _sys
from sockat._place import Config, Env, Kind, LinkFile, Place, State
from sockat._routes import BindDone, Call
from sockat._sys import DIRECT_MAX, IS_APPLE, IS_LINUX, Op

T = TypeVar("T")

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = Path(sockat.__file__).resolve().parents[1]

NO_PROC = b"/nonexistent/proc"
"""Where /proc is said to be when it is to look absent: a path that does not
exist."""

NO_PLACE = b"/nonexistent/place"
"""A place for B that does not exist, so B cannot be made there."""


HAS_PROC = IS_LINUX and os.path.isdir(b"/proc/self/fd")

ROOT = os.geteuid() == 0
"""Root passes every permission check, so the tests of a refused permission
are skipped as root."""


# ---------------------------------------------------------------------------
# temporary directories and paths
# ---------------------------------------------------------------------------


class TmpDirs:
    """Randomly named directories directly under ``/tmp``, all removed by
    :meth:`cleanup` (whatever modes the test left inside them)."""

    def __init__(self) -> None:
        self._made: list[bytes] = []

    def new(self, under: str = "/tmp") -> bytes:
        """A new directory (0700) directly under ``under``."""
        d = os.fsencode(tempfile.mkdtemp(prefix="spt-", dir=under))
        self._made.append(d)
        return d

    def cleanup(self) -> None:
        for d in self._made:
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
    exactly ``total`` bytes long, and return that socket path."""
    fixed = len(base) + 1 + len(name)
    assert total >= fixed, f"{total} < {fixed}"
    remaining = total - fixed
    d = base
    while remaining > 0:
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


def long_path(base: bytes, name: bytes = b"s.sock", total: int = 160) -> bytes:
    """A socket path of ``total`` bytes (longer than any ``sun_path``) under
    ``base``, its directories made."""
    return nest(base, max(total, len(base) + 1 + len(name) + 2), name)


def deep_dir(base: bytes, length: int) -> bytes:
    """A directory of exactly ``length`` bytes under ``base``, made."""
    p = nest(base, length + 2, b"x")
    d = os.path.dirname(p)
    assert len(d) == length
    return d


def is_socket(p: bytes) -> bool:
    try:
        return stat.S_ISSOCK(os.lstat(p).st_mode)
    except FileNotFoundError:
        return False


def listing(d: bytes) -> list[bytes]:
    return sorted(os.listdir(d))


def tree(d: bytes) -> list[str]:
    """Everything under ``d`` (relative paths; symlinks as ``name -> target``)."""
    out: list[str] = []
    for root, dirs, files in os.walk(d):
        for n in dirs + files:
            full = os.path.join(root, n)
            rel = os.path.relpath(full, d)
            if os.path.islink(full):
                out.append(f"{os.fsdecode(rel)} -> {os.fsdecode(os.readlink(full))}")
            else:
                out.append(os.fsdecode(rel) + ("/" if os.path.isdir(full) else ""))
    return sorted(out)


def dir_id(d: bytes | str) -> tuple[int, int]:
    st = os.stat(d)
    return st.st_dev, st.st_ino


# ---------------------------------------------------------------------------
# environments
# ---------------------------------------------------------------------------


def env_at(
    *places: bytes,
    proc: bytes | None = b"/proc" if IS_LINUX else None,
    thread_cwd: bool = IS_LINUX or IS_APPLE,
    link_file: LinkFile | None = None,
    wide: bool = IS_APPLE,
) -> Env:
    """An :class:`Env` of its own (a fresh :class:`State`) whose places for B
    are ``places`` (each as ``tmp`` with B named ``sockat-<uid>``), with
    /proc, the thread's own cwd, link-file and the widened sockaddr as
    given (this system's unless given)."""
    uid = os.geteuid()
    kinds = (Kind.TMP, Kind.RUNTIME, Kind.TMPDIR)
    ps = tuple(Place(kinds[min(i, 2)], p, b"sockat-%d" % uid) for i, p in enumerate(places))
    cfg = Config(places=ps, uid=uid, proc=proc, thread_cwd=thread_cwd, wide=wide)
    if link_file is not None:
        cfg = dataclasses.replace(cfg, link_file=link_file)
    return Env(cfg, State())


def base_of(env: Env) -> bytes:
    """The real path of the process's B in ``env`` (made if need be)."""
    b = env.auto_base()
    assert b is not None
    return b.real


def ds(base: bytes) -> list[bytes]:
    """The D in B ``base``."""
    return sorted(n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n)))


def bind_in(env: Env, path: bytes, **kw: object) -> BindDone:
    """Bind (and listen) ``path`` in ``env``."""
    done, _ = _calls.bind(path, env=env, **kw)  # type: ignore[arg-type]
    return done


def connect_in(
    env: Env, path: bytes, *, nonblocking: bool = False, order: tuple[str, ...] | None = None
) -> tuple[socket.socket, str]:
    """Connect a stream to ``path`` in ``env`` (``order`` forces the routes)."""
    return _calls.connect(path, call=Call(Op.CONNECT, nonblocking=nonblocking), env=env, order=order)


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
    its client."""
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
    """The address the kernel reports for ``s`` itself (past what
    :class:`PathSocket` reports in its place)."""
    addr = socket.socket.getsockname(s)
    assert isinstance(addr, (str, bytes)), addr
    return addr


def kernel_peername(s: socket.socket) -> str | bytes:
    """The address the kernel reports for the peer of ``s``."""
    addr = socket.socket.getpeername(s)
    assert isinstance(addr, (str, bytes)), addr
    return addr


def raises_errno(code: int) -> contextlib.AbstractContextManager[pytest.ExceptionInfo[OSError]]:
    """The block must raise an :class:`OSError` with errno ``code`` (for the
    errnos Python has no subclass for, such as ``EADDRINUSE`` and
    ``ENAMETOOLONG``)."""
    return pytest.raises(OSError, check=lambda e: e.errno == code)


def at_once(f: Callable[[], T]) -> T:
    """Run ``f`` on another thread and return its result (or raise its
    exception), failing the test if it has not returned within 10 seconds
    (the calls run through this return at once; the 10 seconds stand for
    "never returns", not for an estimate of a duration)."""
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


@functools.cache
def standard_errno(case: str, kind: int = socket.SOCK_STREAM) -> int:
    """The errno the standard (the ``socket`` module, a short path) gives on
    this system for ``case``: sockat passes the OS's errors through as they
    are (SPEC "エラー"), and they differ by system (illumos answers a datagram
    connect to a socket nobody holds with ``EDESTADDRREQ``, not
    ``ECONNREFUSED``), so the tests compare with this rather than a fixed
    errno.

    * ``connect-missing``: connect to a path with nothing there
    * ``connect-stale``: connect to a socket file nobody holds
    * ``bind-exists``: bind where a file is already
    * ``bind-no-directory``: bind in a directory that is not there
    * ``bind-file-in-path``: bind under a regular file
    * ``full-backlog``: a nonblocking connect while the listener's backlog is
      full
    """
    d = tempfile.mkdtemp(prefix="spt-std-", dir="/tmp")
    p = os.path.join(d, "s.sock")
    try:
        if case == "full-backlog":
            return _standard_full_backlog(p)
        with socket.socket(socket.AF_UNIX, kind) as s:
            try:
                if case == "connect-missing":
                    s.connect(p)
                elif case == "connect-stale":
                    with socket.socket(socket.AF_UNIX, kind) as held:
                        held.bind(p)
                    s.connect(p)
                elif case == "bind-exists":
                    with open(p, "wb"):
                        pass
                    s.bind(p)
                elif case == "bind-no-directory":
                    s.bind(os.path.join(d, "none", "s.sock"))
                elif case == "bind-file-in-path":
                    with open(os.path.join(d, "f"), "wb"):
                        pass
                    s.bind(os.path.join(d, "f", "s.sock"))
                else:
                    raise AssertionError(case)
            except OSError as e:
                return _errno_of(e)
        raise AssertionError(f"the standard did not fail: {case}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _errno_of(e: OSError) -> int:
    if e.errno is None:
        raise AssertionError(f"no errno: {e!r}")
    return e.errno


def errno_of(call: Callable[[], object]) -> int:
    """The errno ``call`` fails with (a standard socket's call, to compare
    sockat's with)."""
    try:
        call()
    except OSError as e:
        return _errno_of(e)
    raise AssertionError("no error")


def _standard_full_backlog(p: str) -> int:
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(p)
        listener.listen(1)
        queued: list[socket.socket] = []
        try:
            for _ in range(16):
                c = socket.socket(socket.AF_UNIX)
                c.setblocking(False)
                try:
                    c.connect(p)
                except OSError as e:
                    c.close()
                    return _errno_of(e)
                queued.append(c)
        finally:
            for c in queued:
                c.close()
    raise AssertionError("the standard's backlog never filled")


def listener_at(path: bytes) -> socket.socket:
    """A listener on a socket file at ``path``, bound by the standard under a
    short name and renamed to ``path`` (same file system under ``/tmp``): for
    listeners that must not depend on the routes under test."""
    short_dir = tempfile.mkdtemp(prefix="spt-l-", dir="/tmp")
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


def stale_socket(path: bytes) -> None:
    """Leave a socket file at ``path`` that no process listens on (bound
    under a short name, renamed, closed)."""
    listener_at(path).close()
    assert is_socket(path)


def thread_cwd_usable() -> bool:
    """Whether a throwaway thread gets a cwd of its own here. Where the
    answer is known in advance it is asserted, so a broken probe cannot
    quietly move the tests to the "not usable" side: on macOS
    ``pthread_fchdir_np`` is there, and the docker recipes set
    ``SOCKAT_TEST_THREAD_CWD`` to ``1`` (``seccomp=unconfined``) or ``0``
    (the default seccomp)."""
    if not (IS_LINUX or IS_APPLE):
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
    want = os.environ.get("SOCKAT_TEST_THREAD_CWD")
    if want is not None:
        assert usable == (want == "1"), f"SOCKAT_TEST_THREAD_CWD={want} but the probe says {usable}"
    if sys.platform == "darwin":
        assert usable, "pthread_fchdir_np is expected on macOS"
    return usable


THREAD_CWD = thread_cwd_usable()

HAS_THREAD_ROUTE = sys.platform.startswith("freebsd") or THREAD_CWD
"""Whether a bind has thread-cwd (or at, FreeBSD) here."""

THREAD_ROUTE_IN_BIND_ROW = IS_LINUX or IS_APPLE or sys.platform.startswith("freebsd")
"""Whether this system's bind row has thread-cwd (or at, FreeBSD): the SPEC
leaves a route the system has no means for out of its row (NetBSD, OpenBSD,
illumos), so out of skipped too."""

THREAD_CWD_DIRECT_IN_CONNECT_ROW = IS_LINUX or IS_APPLE
"""Whether this system's connect row has thread-cwd-direct (a thread can be
given a cwd of its own: Linux, Apple)."""

NAME_MAX_THREAD_CWD = DIRECT_MAX - 11
"""The longest file name thread-cwd / at bind (``E/<name>``, E 10 bytes)."""


# ---------------------------------------------------------------------------
# child processes
# ---------------------------------------------------------------------------

RESULT_MARK = "sockat-child: "

PRIVATE_PLACE_ENV = "SOCKAT_TEST_PRIVATE_PLACE"
"""The environment variable that hands the session's own place for B to the
child processes (see ``conftest._private_place``)."""


def private_config(place: bytes) -> Config:
    """The configuration the public calls get in the tests: the session's own
    place for B, the rest as read from the system."""
    return Config(places=(Place(Kind.TMP, place, b"sockat-%d" % os.geteuid()),))


PRIVATE_PLACE_CODE = f"""
import os as _os
if _os.environ.get({PRIVATE_PLACE_ENV!r}):
    from sockat import _place as _p
    _place_dir = _os.fsencode(_os.environ[{PRIVATE_PLACE_ENV!r}])
    _p.Config.from_env = classmethod(
        lambda cls: _p.Config(places=(_p.Place(_p.Kind.TMP, _place_dir, b"sockat-%d" % _os.geteuid()),))
    )
"""
"""Code that a child process runs first so that its public calls use the
session's own place too."""

PRELUDE = (
    f"""
import json, os, socket, sys
sys.path[:0] = [{str(SRC_DIR)!r}, {str(TESTS_DIR)!r}]
def report(**values):
    print({RESULT_MARK!r} + json.dumps(values), flush=True)
"""
    + PRIVATE_PLACE_CODE
)


def run_child(
    code: str,
    cwd: bytes = b"/",
    env: Mapping[str, str | bytes] | None = None,
    *,
    args: Mapping[str, object] | None = None,
    check: bool = True,
) -> dict[str, object]:
    """Run the Python ``code`` (dedented) in a child process of this Python,
    with ``ARGS`` (``args`` as a dict decoded from JSON) and ``report(**kw)``
    defined, cwd ``cwd`` (``$PWD`` the same), and return what it reported
    (``{"returncode": n}`` added). The child must exit 0 unless not
    ``check``."""
    child_env: dict[bytes, bytes] = dict(os.environb)
    child_env[b"PWD"] = cwd
    for k, v in (env or {}).items():
        child_env[os.fsencode(k)] = os.fsencode(v)
    prelude = PRELUDE + f"ARGS = json.loads({json.dumps(json.dumps(args or {}))})\n"
    out = subprocess.run(
        [sys.executable, "-c", prelude + textwrap.dedent(code)],
        cwd=cwd,
        env=child_env,
        capture_output=True,
        check=False,
    )
    stdout = out.stdout.decode(errors="replace")
    stderr = out.stderr.decode(errors="replace")
    if check:
        assert out.returncode == 0, f"child failed ({out.returncode})\n--- stdout\n{stdout}\n--- stderr\n{stderr}"
    result: dict[str, object] = {"returncode": out.returncode, "stderr": stderr}
    for line in stdout.splitlines():
        if line.startswith(RESULT_MARK):
            parsed = json.loads(line[len(RESULT_MARK) :])
            assert isinstance(parsed, dict)
            result.update(parsed)
    return result
