"""Bind / connect Unix domain sockets at paths longer than ``sun_path``.

Use :func:`bind` / :func:`connect` where you would create an ``AF_UNIX``
socket and call ``bind`` + ``listen`` / ``connect`` on it::

    import sockat

    listener = sockat.bind("/a/very/deep/directory/.../server.sock")
    client = sockat.connect("/a/very/deep/directory/.../server.sock")

:func:`bind_ex` also gives a function that removes the socket file it made,
and nothing else::

    listener, discard = sockat.bind_ex("/a/very/deep/directory/.../server.sock")
    ...
    listener.close()
    discard()

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
``ENAMETOOLONG``: for a bind, a file name longer than the central route
carries (or, on systems where it has no means, any long path).
"""

from __future__ import annotations

import os
import socket
import sys
from collections.abc import Callable

if sys.platform == "win32":  # pragma: no cover
    raise ImportError("sockat supports Unix systems only")

from . import _socket_file
from ._route import BIND, CONNECT, CONNECT_NONBLOCKING, StrOrBytesPath, run

__all__ = ["bind", "bind_ex", "connect", "connect_nonblocking"]


def bind(path: StrOrBytesPath) -> socket.socket:
    """Create an ``AF_UNIX`` stream socket, bind it to ``path`` and listen
    with the :mod:`socket` module's default backlog.

    The same as ``socket.socket(AF_UNIX, SOCK_STREAM)`` followed by
    ``bind(path)`` and ``listen()``, for any length of ``path``. The socket
    file is not removed when the socket is closed (as with the standard
    library); remove it with ``os.unlink(path)``.
    """
    return run(BIND, path)[0]


def bind_ex(path: StrOrBytesPath) -> tuple[socket.socket, Callable[[], None]]:
    """:func:`bind`, and a function ``discard`` that removes the socket file
    the bind made: ``listener, discard = sockat.bind_ex(path)``.

    Right after the bind goes through, before the listener is returned, the
    realpath of the socket file and its ``(st_dev, st_ino)`` are remembered
    (when that fails, the listener is closed and the error raised). ``discard()`` removes the file at
    that realpath only while it is still that file: when it is gone, or has
    been replaced by another file, it does nothing. It may be called any
    number of times. It does not close the listener (close it yourself, or
    let asyncio close it when it serves it). Errors other than the file being
    gone are raised as :class:`OSError`.

    For a long path, the standard library's own ways of removing the socket
    file (asyncio's server cleanup reads the listener's address) work too,
    since the listener reports the path given; ``discard`` is the way that
    does not depend on the reader.
    """
    # The path is read once (os.fspath), and that one value is what is bound
    # and remembered: a path-like object may answer differently each time.
    spelled = os.fspath(path)
    with _socket_file.recording(spelled) as recorder:
        listener = bind(spelled)
    discard = recorder.discard
    if discard is None:  # every route records right after its bind
        listener.close()
        raise RuntimeError("the bind did not record the socket file it made")
    return listener, discard


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
