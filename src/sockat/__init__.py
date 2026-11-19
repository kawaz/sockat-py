"""Bind / connect Unix domain sockets at paths longer than ``sun_path``.

Use :func:`bind` / :func:`connect` where you would create an ``AF_UNIX``
socket and call ``bind`` + ``listen`` / ``connect`` on it::

    import sockat

    listener = sockat.bind("/a/very/deep/directory/.../server.sock")
    client = sockat.connect("/a/very/deep/directory/.../server.sock")

* The return value is a :class:`socket.socket`. For a long path it is a
  subclass whose ``getsockname()`` (listener, accepted sockets) and
  ``getpeername()`` (connected stream) report the path given, as the
  standard library reports a short one, so code that reads the address back
  (asyncio's server cleanup) works on the path.
* A path the :mod:`socket` module accepts (``sun_path`` less 1 byte: 107 on
  Linux, 103 on macOS and the BSDs) goes to it unchanged, so it behaves as the
  standard library does.
* A longer path is reached by the routes of the sockat SPEC
  (https://github.com/kawaz/sockat/blob/main/docs/SPEC.md), and the socket
  file is created at the given path.

Errors are :class:`OSError` with the ``errno`` of the system call (so its
subclasses such as :class:`FileNotFoundError`), :class:`ValueError` for a
long path that does not end with a file name or contains a NUL byte, and an
:class:`OSError` without ``errno`` when the cwd changed during a connect
through a path relative to it. A long path that no route reaches is
``ENAMETOOLONG``: for a bind where the bind does not go through a symlink to
the socket file (it does on macOS, whatever the file name; not on Linux), a
file name longer than the routes through D carry (or, on systems where they
have no means, any long path).

:mod:`sockat.ex` (imported only when asked for: ``import sockat.ex``) has
the same three calls with what they went through: the route taken, what it
lost and the routes skipped, a function that removes the socket file the
bind made and nothing else, and, when asked, a last resort that reaches the
path even by losing something.
"""

from __future__ import annotations

import socket
import sys

if sys.platform == "win32":  # pragma: no cover
    raise ImportError("sockat supports Unix systems only")

from ._route import BIND, CONNECT, CONNECT_NONBLOCKING, StrOrBytesPath, run

__all__ = ["bind", "connect", "connect_nonblocking"]


def bind(path: StrOrBytesPath) -> socket.socket:
    """Create an ``AF_UNIX`` stream socket, bind it to ``path`` and listen
    with the :mod:`socket` module's default backlog.

    The same as ``socket.socket(AF_UNIX, SOCK_STREAM)`` followed by
    ``bind(path)`` and ``listen()``, for any length of ``path``. The socket
    file is not removed when the socket is closed (as with the standard
    library); remove it with ``os.unlink(path)``.
    """
    return run(BIND, path)[0]


def connect(path: StrOrBytesPath) -> socket.socket:
    """Create an ``AF_UNIX`` stream socket and connect it to ``path``
    (blocking).

    The same as ``socket.socket(AF_UNIX, SOCK_STREAM)`` followed by
    ``connect(path)``, for any length of ``path``.
    """
    return run(CONNECT, path)[0]


def connect_nonblocking(path: StrOrBytesPath) -> socket.socket:
    """Like :func:`connect`, but the socket is made nonblocking before the
    connect and returned so, ready for an event loop (for example
    ``asyncio.open_unix_connection(sock=...)``).

    A Unix-domain connect completes or fails within the call, so nothing is
    left to wait for. When the listener's backlog is full it fails at once
    (Linux: :class:`BlockingIOError`, macOS: :class:`ConnectionRefusedError`).
    """
    return run(CONNECT_NONBLOCKING, path)[0]
