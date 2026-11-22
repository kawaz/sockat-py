"""The boundary to the C library. Every call that the ``socket`` module cannot
make (an address it would refuse, a system call it does not wrap) goes
through :mod:`ctypes` here, and only here.

Contents:

* :data:`SUN_PATH_CAP` / :data:`DIRECT_MAX` — the length of ``sun_path`` on
  this system and the longest path the ``socket`` module takes.
* :func:`sockaddr` — a ``sockaddr_un`` built by hand, in the standard form or
  (Apple) the widened form whose path runs past ``sun_path`` up to
  :data:`WIDE_PATH_MAX` bytes.
* :func:`op` — ``bind(2)`` / ``connect(2)`` of a ``socket.socket`` with such an
  address.
* :func:`op_at` — ``bindat(2)`` / ``connectat(2)`` (FreeBSD).
* :func:`getcwd` — the cwd, read with ``PATH_MAX`` as the limit (a bounded
  buffer on illumos).
* :func:`thread_cwd_possible` / :func:`enter_dir_in_this_thread` — a cwd of
  the calling thread's own (Linux: ``unshare(CLONE_FS)``, Apple:
  ``pthread_fchdir_np``).

:mod:`ctypes` is imported on first use, so a process that never needs these
calls never loads it.
"""

from __future__ import annotations

import errno
import os
import select
import socket
import struct
import sys
from enum import Enum
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import ctypes


class Op(Enum):
    """What to do with a socket and an address."""

    BIND = "bind"
    """``bind(2)``; the caller listens afterwards."""
    CONNECT = "connect"
    """``connect(2)``."""


def _platform_family() -> str:
    """The family of ``sys.platform`` that decides the ``sockaddr_un`` layout."""
    p = sys.platform
    for prefix in ("darwin", "ios", "freebsd", "netbsd", "openbsd", "dragonfly"):
        if p.startswith(prefix):
            return "bsd"
    return p


IS_APPLE = sys.platform in ("darwin", "ios")
"""Apple's kernel (XNU), which takes the widened ``sockaddr_un``."""
IS_LINUX = sys.platform.startswith("linux") or sys.platform == "android"
"""Linux and Android, which have ``/proc/self/fd``."""
IS_FREEBSD = sys.platform.startswith("freebsd")
"""FreeBSD, which has ``bindat(2)`` / ``connectat(2)``."""
_BSD_LAYOUT = _platform_family() == "bsd"
"""``sockaddr_un`` starts with a 1-byte ``sun_len`` then a 1-byte family (the
BSDs and Apple); elsewhere with a 2-byte family."""

SUN_PATH_OFFSET = 2
"""Offset of ``sun_path`` in ``struct sockaddr_un`` (2 on every supported
system)."""

_SUN_PATH_108 = IS_LINUX or sys.platform.startswith("sunos")
"""Linux, Android and illumos, whose ``sun_path`` is 108 bytes."""

SUN_PATH_CAP = 108 if _SUN_PATH_108 else 104
"""Length of the ``sun_path`` array of ``struct sockaddr_un``: 108 on Linux,
Android and illumos, 104 on the BSDs and Apple. Another Unix is taken as 104,
the smaller (DR-0001: a path the standard library would take then goes
through a route instead, rather than one it refuses being handed to it). The ``socket`` module refuses a
path of this many bytes or more (``path.len >= sizeof sun_path`` in CPython's
``socketmodule.c``), so a path fits the standard form when it is shorter."""

DIRECT_MAX = SUN_PATH_CAP - 1
"""The longest path the ``socket`` module takes, ``sun_path`` less the NUL:
107 on Linux, Android and illumos, 103 on Apple and the BSDs."""

WIDE_PATH_MAX = 253
"""Longest path the widened ``sockaddr_un`` carries on Apple systems.

XNU accepts a socket address of up to ``SOCK_MAXADDRLEN`` (255) bytes in
``bind(2)`` / ``connect(2)`` and takes ``sun_len - offsetof(sun_path)`` bytes
as the path, requiring that to be less than ``SOCK_MAXADDRLEN``. With the
length byte and the family byte in front, 253 bytes of path fit, without a
NUL. libuv uses the same layout (#5047)."""


def _check_bytes(path: bytes) -> None:
    if not path:
        raise ValueError("socket path is empty")
    if b"\0" in path:
        raise ValueError("socket path must not contain a NUL byte")


def _enametoolong() -> OSError:
    return OSError(errno.ENAMETOOLONG, os.strerror(errno.ENAMETOOLONG))


def sockaddr(path: bytes, *, wide: bool = False) -> bytes:
    """The bytes of a ``sockaddr_un`` holding ``path``.

    Standard form: ``path`` must be shorter than :data:`SUN_PATH_CAP`, and the
    address carries a terminating NUL (the bytes the ``socket`` module builds).

    Widened form (``wide``, Apple only): up to :data:`WIDE_PATH_MAX` bytes of
    path, no terminating NUL, ``sun_len`` and the address length both
    ``2 + len(path)``.

    Raises:
        ValueError: ``path`` is empty or contains a NUL byte.
        OSError: ``ENAMETOOLONG`` when it does not fit.
    """
    _check_bytes(path)
    if wide:
        if not IS_APPLE:
            raise AssertionError("the widened sockaddr_un exists on Apple systems only")
        if len(path) > WIDE_PATH_MAX:
            raise _enametoolong()
        length = SUN_PATH_OFFSET + len(path)
        body = path
    else:
        if len(path) >= SUN_PATH_CAP:
            raise _enametoolong()
        length = SUN_PATH_OFFSET + len(path) + 1
        body = path + b"\0"
    header = struct.pack("=BB", length, socket.AF_UNIX) if _BSD_LAYOUT else struct.pack("=H", socket.AF_UNIX)
    return header + body


_libc: ctypes.CDLL | None = None


def _c() -> ctypes.CDLL:
    """The C library, loaded on first use with ``errno`` captured per call."""
    global _libc
    if _libc is None:
        import ctypes as c

        _libc = c.CDLL(None, use_errno=True)
    return _libc


def _raise_errno(err: int) -> None:
    raise OSError(err, os.strerror(err))


def _wait_connected(sock: socket.socket) -> None:
    """Finish a ``connect(2)`` that was interrupted by a signal, as the
    ``socket`` module does (bpo-23618): the connection goes on in the kernel,
    so wait until the socket is writable (within the socket's timeout) and read
    the outcome from ``SO_ERROR``."""
    timeout = sock.gettimeout()
    poller = select.poll()
    poller.register(sock.fileno(), select.POLLOUT)
    ready = poller.poll(None if timeout is None else timeout * 1000)
    if not ready:
        raise TimeoutError("timed out")
    err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    if err:
        _raise_errno(err)


def op(which: Op, sock: socket.socket, addr: bytes) -> None:
    """``bind(2)`` or ``connect(2)`` on ``sock`` with the address bytes
    ``addr`` (from :func:`sockaddr`).

    A connect interrupted by a signal (``EINTR``) is finished as the
    ``socket`` module finishes its own: a blocking socket waits for the
    outcome, a nonblocking one raises ``InterruptedError``.
    """
    import ctypes as c

    fn = _c().bind if which is Op.BIND else _c().connect
    buf = c.create_string_buffer(addr, len(addr))
    rc: int = fn(c.c_int(sock.fileno()), buf, c.c_uint32(len(addr)))
    if rc == 0:
        return
    err = c.get_errno()
    if which is Op.CONNECT and err == errno.EINTR and sock.gettimeout() != 0.0:
        _wait_connected(sock)
        return
    _raise_errno(err)


def op_at(which: Op, sock: socket.socket, dirfd: int, addr: bytes) -> None:
    """``bindat(2)`` / ``connectat(2)`` with ``addr`` resolved relative to the
    directory ``dirfd`` (FreeBSD 10+), with the same ``EINTR`` handling as
    :func:`op`."""
    import ctypes as c

    fn = _c().bindat if which is Op.BIND else _c().connectat
    buf = c.create_string_buffer(addr, len(addr))
    rc: int = fn(c.c_int(dirfd), c.c_int(sock.fileno()), buf, c.c_uint32(len(addr)))
    if rc == 0:
        return
    err = c.get_errno()
    if which is Op.CONNECT and err == errno.EINTR and sock.gettimeout() != 0.0:
        _wait_connected(sock)
        return
    _raise_errno(err)


IS_ILLUMOS = sys.platform.startswith("sunos")
"""illumos (and Solaris), whose ``getcwd(3C)`` is asked with a bounded
buffer (:func:`getcwd`)."""


def path_max() -> int:
    """``PATH_MAX`` of this system (``pathconf("/", _PC_PATH_MAX)``; 4096
    when it does not say): the longest path a call takes, NUL included."""
    try:
        n = os.pathconf("/", "PC_PATH_MAX")
    except (OSError, ValueError):
        return 4096
    return n if n > 0 else 4096


def getcwd() -> bytes:
    """The cwd, read with ``PATH_MAX`` as the limit: a cwd that does not fit
    (``PATH_MAX`` bytes or more) is an ``OSError`` (``ENAMETOOLONG``, or
    ``ERANGE`` from illumos's ``getcwd(3C)``), the SPEC's "the realpath
    cannot be had" (step 5), decided here for every system.

    Design rationale: CPython's ``os.getcwd`` (``posix_getcwd`` in
    Modules/posixmodule.c, 3.13) calls ``getcwd(3)`` again with a buffer
    1024 bytes larger each time it answers ``ERANGE``, until it succeeds or
    the allocation fails (``MemoryError``). In a cwd past ``MAXPATHLEN`` on
    illumos it never succeeds (inferred from that source and the illumos CI
    run of 0.4.0, which ran for 13 minutes and ended in ``MemoryError``; no
    illumos machine to try it on by hand). So on illumos ``getcwd(3C)`` is
    called once here with a ``PATH_MAX`` buffer. Elsewhere ``getcwd(3)``
    returns or fails with an errno (OpenBSD: ``ENAMETOOLONG``), and a result
    of ``PATH_MAX`` bytes or more is cut off here the same way."""
    limit = path_max()
    if not IS_ILLUMOS:
        cwd = os.getcwdb()
        if len(cwd) >= limit:
            _raise_errno(errno.ENAMETOOLONG)
        return cwd
    fn, c = _getcwd_c()
    buf = c.create_string_buffer(limit)
    got: bytes | None = fn(buf, c.c_size_t(limit))
    if got is None:
        _raise_errno(c.get_errno() or errno.ERANGE)
    assert got is not None
    return got


_getcwd_fn: tuple[ctypes._NamedFuncPointer, ModuleType] | None = None


def _getcwd_c() -> tuple[ctypes._NamedFuncPointer, ModuleType]:
    """``getcwd(3)`` of the C library through :mod:`ctypes`, and the
    :mod:`ctypes` module, made ready once."""
    global _getcwd_fn
    if _getcwd_fn is None:
        import ctypes as c

        fn = _c().getcwd
        fn.restype = c.c_char_p
        fn.argtypes = [c.c_char_p, c.c_size_t]
        _getcwd_fn = (fn, c)
    return _getcwd_fn


if IS_ILLUMOS:
    # Design rationale: on illumos, ctypes is imported when this module is,
    # not on first use as elsewhere (AGENTS.md). Importing a module not yet
    # imported looks up the '' entry of sys.path (``python -c``, the
    # interactive interpreter) with os.getcwd() (importlib's
    # PathFinder._path_importer_cache), which in a cwd past MAXPATHLEN on
    # illumos ends in MemoryError (see getcwd). Made ready here, the first
    # getcwd() of a call made in such a cwd imports nothing. A program that
    # imports sockat itself for the first time in such a cwd still fails in
    # that import: that is the import system's, which sockat cannot help.
    _getcwd_c()


HAS_THREAD_CWD = IS_LINUX or IS_APPLE
"""Whether this system has a way to give one thread a cwd of its own."""

_CLONE_FS = 0x200
"""``CLONE_FS`` of ``<sched.h>`` (Linux)."""

_PR_GET_SECCOMP = 21
"""``PR_GET_SECCOMP`` of ``<linux/prctl.h>``."""


def _seccomp_filter_off() -> bool:
    """Linux: whether ``prctl(PR_GET_SECCOMP)`` answers 0 (no seccomp filter
    on the calling thread, which a thread it starts inherits)."""
    import ctypes as c

    rc: int = _c().prctl(c.c_int(_PR_GET_SECCOMP), c.c_ulong(0), c.c_ulong(0), c.c_ulong(0), c.c_ulong(0))
    return rc == 0


def thread_cwd_possible() -> bool:
    """Whether :func:`enter_dir_in_this_thread` may succeed, as far as it can
    be told without a thread (before anything is made for it): Linux, no
    seccomp filter on; Apple, ``pthread_fchdir_np`` is there. ``unshare``
    itself can still be refused in the thread."""
    if IS_LINUX:
        return _seccomp_filter_off()
    if IS_APPLE:
        return hasattr(_c(), "pthread_fchdir_np")
    return False


def enter_dir_in_this_thread(dirfd: int) -> bool:
    """Make the directory ``dirfd`` the cwd of the calling thread only, and
    ``True``; ``False`` when the system will not give the thread a cwd of its
    own. The process cwd, which every other thread keeps using, is not
    changed. Call it only from a thread made for the purpose and thrown away
    afterwards: the thread keeps that cwd until it ends.

    * Linux: ``unshare(CLONE_FS)`` (the thread stops sharing the cwd with the
      rest of the process), then ``fchdir(dirfd)`` (its failure is raised).
      ``False`` without calling ``unshare`` when a seccomp filter is on
      (``prctl(PR_GET_SECCOMP)`` is not 0): a filter may answer ``unshare``
      by killing the process (``SIGSYS``, as systemd's ``SystemCallFilter=``
      does) rather than with an error. ``False`` also when ``unshare`` is
      refused.
    * Apple: ``pthread_fchdir_np(dirfd)``, a private libSystem function looked
      up at run time (``False`` when it is missing or fails).
    """
    import ctypes as c

    if IS_LINUX:
        if not _seccomp_filter_off():
            return False
        if _c().unshare(c.c_int(_CLONE_FS)) != 0:
            return False
        os.fchdir(dirfd)
        return True
    if IS_APPLE:
        try:
            fchdir_np = _c().pthread_fchdir_np
        except AttributeError:
            return False
        rc: int = fchdir_np(c.c_int(dirfd))
        return rc == 0
    return False
