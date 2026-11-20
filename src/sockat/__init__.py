"""Bind / connect Unix domain sockets at paths longer than ``sun_path``.

Use :func:`bind` / :func:`connect` where you would create an ``AF_UNIX``
socket and call ``bind`` + ``listen`` / ``connect`` on it::

    import sockat

    listener = sockat.bind("/a/very/deep/directory/.../server.sock")
    client = sockat.connect("/a/very/deep/directory/.../server.sock")

* The return value is a :class:`socket.socket`. For a long path it is a
  subclass whose ``getsockname()`` (listener, accepted sockets) and
  ``getpeername()`` (connected stream) report the path given, as the
  standard library reports a short one.
* A path the :mod:`socket` module accepts (``sun_path`` less 1 byte: 107 on
  Linux, 103 on macOS and the BSDs) goes to it unchanged.
* A longer path is reached by the routes of the sockat SPEC
  (https://github.com/kawaz/sockat/blob/main/docs/SPEC.md): a bind through a
  short spelling that keeps naming the path (a symlink in a directory kept in
  ``/tmp``, ``$XDG_RUNTIME_DIR`` or ``$TMPDIR``), a connect through the
  routes that need no write first and leaving nothing behind.

:mod:`sockat.datagram` has the same for ``SOCK_DGRAM``. :mod:`sockat.ex`
(imported only when asked for: ``import sockat.ex``) has the calls with what
they went through, ``remove()`` / ``cleanup()``, the resolve calls and
``sweep()``.

Errors are :class:`OSError` with the ``errno`` of the system call (so its
subclasses such as :class:`FileNotFoundError`), :class:`ValueError` for a
long path that does not end with a file name or a path with a NUL byte, and
an :class:`OSError` without ``errno`` when the cwd changed during a connect
through a path relative to it. A long path that no route reaches is
``ENAMETOOLONG``.
"""

from __future__ import annotations

import socket
import sys

if sys.platform == "win32":  # pragma: no cover
    raise ImportError("sockat supports Unix systems only")

from . import _calls, datagram
from ._routes import Call, StrOrBytesPath
from ._sys import Op

__all__ = ["bind", "connect", "connect_nonblocking", "datagram"]


def bind(path: StrOrBytesPath) -> socket.socket:
    """Create an ``AF_UNIX`` stream socket, bind it to ``path`` and listen
    with the :mod:`socket` module's default backlog.

    The same as ``socket.socket(AF_UNIX, SOCK_STREAM)`` followed by
    ``bind(path)`` and ``listen()``, for any length of ``path``. The socket
    file is not removed when the socket is closed; remove it by ``path``.
    For a long path the spelling the kernel keeps stays usable for the life
    of the process (a symlink left in sockat's directory); use
    :func:`sockat.ex.bind` to clean it up.
    """
    return _calls.bind(path)[0].sock


def connect(path: StrOrBytesPath) -> socket.socket:
    """Create an ``AF_UNIX`` stream socket and connect it to ``path``
    (blocking), for any length of ``path``. Nothing is left behind."""
    return _calls.connect(path, call=Call(Op.CONNECT))[0]


def connect_nonblocking(path: StrOrBytesPath) -> socket.socket:
    """Like :func:`connect`, but the socket is made nonblocking before the
    connect and returned so, ready for an event loop (for example
    ``asyncio.open_unix_connection(sock=...)``).

    A Unix-domain connect completes or fails within the call, so nothing is
    left to wait for. When the listener's backlog is full it fails at once
    (Linux: :class:`BlockingIOError`, macOS: :class:`ConnectionRefusedError`).
    """
    return _calls.connect(path, call=Call(Op.CONNECT, nonblocking=True))[0]
