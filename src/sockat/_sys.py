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
* :func:`enter_dir_in_this_thread` / :func:`leave_dir_in_this_thread` — a cwd
  of the calling thread's own (Linux: ``unshare(CLONE_FS)``, Apple:
  ``pthread_fchdir_np``).

:mod:`ctypes` is imported on first use, so a system that never needs these
calls (Linux, whose long paths go through /proc) never loads it.
"""

from __future__ import annotations

import errno
import os
import select
import socket
import struct
import sys
from enum import Enum
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

SUN_PATH_CAP = 104 if _BSD_LAYOUT else 108
"""Length of the ``sun_path`` array of ``struct sockaddr_un``: 104 on the BSDs
and Apple, 108 on Linux, Android and illumos. The ``socket`` module refuses a
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


HAS_THREAD_CWD = IS_LINUX or IS_APPLE
"""Whether this system has a way to give one thread a cwd of its own."""

_CLONE_FS = 0x200
"""``CLONE_FS`` of ``<sched.h>`` (Linux)."""


def enter_dir_in_this_thread(dirfd: int) -> bool:
    """Make the directory ``dirfd`` the cwd of the calling thread only, and
    ``True``; ``False`` when the system will not give the thread a cwd of its
    own. The process cwd, which every other thread keeps using, is not
    changed.

    * Linux: ``unshare(CLONE_FS)`` (the thread stops sharing the cwd with the
      rest of the process; ``False`` when refused, as docker's default seccomp
      does with ``EPERM``), then ``fchdir(dirfd)`` (its failure is raised).
      This cannot be undone: the thread must be thrown away afterwards.
    * Apple: ``pthread_fchdir_np(dirfd)``, a private libSystem function looked
      up at run time (``False`` when it is missing or fails).
    """
    import ctypes as c

    if IS_LINUX:
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


def leave_dir_in_this_thread() -> None:
    """Undo :func:`enter_dir_in_this_thread` where that can be done (Apple:
    ``pthread_fchdir_np(-1)`` returns the thread to the process cwd). On Linux
    the thread keeps its own cwd until it ends."""
    if IS_APPLE:
        import ctypes as c

        _c().pthread_fchdir_np(c.c_int(-1))
