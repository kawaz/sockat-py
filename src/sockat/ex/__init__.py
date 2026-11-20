"""The calls of :mod:`sockat` with what they went through (the sockat SPEC's
``_ex``), the resolve calls for those who hand a short spelling to the
standard library themselves, and :func:`sweep`.

``import sockat`` does not import this module: using it is a choice, made by
importing it. Call it through the module, so that it is not mistaken for
the plain calls (nor for the standard ``socket.connect_ex``)::

    import sockat.ex

    bound = sockat.ex.bind(path)
    match bound.spelling:
        case sockat.ex.Linked(removable=False) | sockat.ex.Dangling():
            ...  # remove the socket file by the path given, never by the address
    server = await asyncio.start_unix_server(handle, sock=bound.socket)
    ...
    bound.file.remove()  # before the listener is closed

    connected = sockat.ex.connect(path)

The routes are those of the plain calls, in the same order. A long path's
spelling goes through sockat's directories: B (one per user, kept from
process to process in ``/tmp``, ``$XDG_RUNTIME_DIR`` or ``$TMPDIR``), D (one
per process in B, kept while the process lives) and E (a symlink in D).

:mod:`sockat.ex.datagram` has the same for ``SOCK_DGRAM``.

Errors are those of the plain calls (an :class:`OSError` keeps its type and
``errno``), each with two attributes added: ``skipped`` (as
:attr:`Bound.skipped`, up to the failure) and ``route`` (the name of the
route whose bind / connect failed, or ``None`` when none got that far).
"""

from __future__ import annotations

import os
import socket as _socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import NamedTuple

from .. import _calls
from .._place import Env
from .._routes import Call, StrOrBytesPath
from .._socket_file import SocketFile
from .._spelling import CwdRelative, Dangling, Linked, Original, Skipped, Spelling, Trail, Until
from .._sys import Op

__all__ = [
    "Bound",
    "Connected",
    "CwdRelative",
    "Dangling",
    "ForBind",
    "ForConnect",
    "Linked",
    "Options",
    "Original",
    "Report",
    "Skipped",
    "SocketFile",
    "Spelling",
    "Until",
    "bind",
    "connect",
    "connect_nonblocking",
    "resolve_for_bind",
    "resolve_for_connect",
    "sweep",
]


@dataclass(frozen=True)
class Options:
    """What the ``_ex`` and resolve calls may be asked for."""

    usable_cross_uid: bool = False
    """Ask for a spelling a peer of another uid can follow: B is taken from
    a place others can search their way to (``/tmp``), and the
    ``/proc/<pid>/fd`` spelling is not used. Where that cannot be had, the
    bind goes on to a route whose spelling is :class:`Dangling` rather than
    one other uids cannot follow (see the result's ``spelling``)."""
    base: StrOrBytesPath | None = None
    """B itself, given by the application (a place it manages, such as
    ``/tmp/app-<uid>/sockat``): made there (its parent must exist) or the one
    there checked, and the call fails rather than fall back to the automatic
    places when that fails (``ENOENT`` for a missing parent, ``ENOTDIR``,
    ``EPERM`` when a check fails)."""


def _opts(options: Options | None) -> _calls.Opts:
    if options is None:
        return _calls.PLAIN
    base = None if options.base is None else os.fsencode(os.fspath(options.base))
    return _calls.Opts(usable_cross_uid=options.usable_cross_uid, base=base)


class Bound(NamedTuple):
    """What :func:`bind` returns:
    ``socket, file, spelling, route, base, skipped = sockat.ex.bind(path)``."""

    socket: _socket.socket
    """The listening socket, as :func:`sockat.bind` returns it (for a long
    path, one that reports the path given as its address)."""
    file: SocketFile
    """``file.remove()`` removes the socket file the bind made (and does
    ``cleanup()``); ``file.cleanup()`` removes the E its spelling goes
    through. Neither happens unless called."""
    spelling: Spelling
    """What the spelling the kernel keeps is good for. Branch on this."""
    route: str
    """The route taken, as the sockat SPEC names it. For diagnosis only."""
    base: str | None
    """Which B the spelling goes through (``"proc"``, ``"sockdir"``,
    ``"tmp"``, ``"runtime"``, ``"tmpdir"``, ``"base"``), ``None`` when it
    goes through none. For diagnosis only."""
    skipped: tuple[Skipped, ...]
    """The routes tried before :attr:`route` and found unusable, in order.
    For diagnosis only."""


class Connected(NamedTuple):
    """What :func:`connect` / :func:`connect_nonblocking` return:
    ``socket, route, skipped = sockat.ex.connect(path)``."""

    socket: _socket.socket
    route: str
    skipped: tuple[Skipped, ...]


class Report(NamedTuple):
    """What :func:`sockat.ex.datagram.connect` returns."""

    route: str
    skipped: tuple[Skipped, ...]


@dataclass(frozen=True)
class ForBind:
    """What :func:`resolve_for_bind` returns: a spelling to bind with the
    standard library."""

    path: str | bytes
    """The spelling (``str`` or ``bytes`` as the path was given)."""
    spelling: Spelling
    route: str
    base: str | None
    skipped: tuple[Skipped, ...]
    _cleanup: Callable[[], None] = field(repr=False, compare=False)

    def cleanup(self) -> None:
        """Remove the E the spelling goes through, once. Do not use the
        spelling afterwards."""
        self._cleanup()


@dataclass(frozen=True)
class ForConnect:
    """What :func:`resolve_for_connect` returns: a spelling to connect (or
    send) to with the standard library, as often as needed. It has no
    ``remove()``: the socket file is not the caller's to remove."""

    path: str | bytes
    route: str
    base: str | None
    skipped: tuple[Skipped, ...]
    _cleanup: Callable[[], None] = field(repr=False, compare=False)

    def cleanup(self) -> None:
        """Remove the E the spelling goes through, once. Do not use the
        spelling afterwards."""
        self._cleanup()


def _nothing() -> None:
    pass


@contextmanager
def _telling(trail: Trail) -> Iterator[None]:
    try:
        yield
    except (OSError, ValueError) as e:
        trail.tell(e)
        raise


def bind(path: StrOrBytesPath, options: Options | None = None) -> Bound:
    """:func:`sockat.bind`, with what it went through and the socket file.

    ``file.remove()`` removes the socket file the bind made, and nothing
    else: its realpath and ``(st_dev, st_ino)`` are remembered right after
    the bind, before the listener is returned (when that fails, the listener
    is closed and the error raised; for a relative path, so is a cwd changed
    between the start of the call and then, an :class:`OSError` without
    ``errno``). Call it before closing the listener (see
    :meth:`SocketFile.remove`)."""
    trail = Trail()
    with _telling(trail):
        done, file = _calls.bind(path, opts=_opts(options), remember=True, trail=trail)
    assert file is not None
    return Bound(done.sock, file, done.spelling, done.route, done.base, tuple(trail.skipped))


def connect(path: StrOrBytesPath, options: Options | None = None) -> Connected:
    """:func:`sockat.connect`, with what it went through."""
    return _connect(Call(Op.CONNECT), path, options)


def connect_nonblocking(path: StrOrBytesPath, options: Options | None = None) -> Connected:
    """:func:`sockat.connect_nonblocking`, with what it went through."""
    return _connect(Call(Op.CONNECT, nonblocking=True), path, options)


def _connect(call: Call, path: StrOrBytesPath, options: Options | None) -> Connected:
    trail = Trail()
    with _telling(trail):
        sock, route = _calls.connect(path, call=call, opts=_opts(options), trail=trail)
    return Connected(sock, route, tuple(trail.skipped))


def resolve_for_bind(path: StrOrBytesPath, options: Options | None = None) -> ForBind:
    """A short spelling that names the same place as ``path`` for as long as
    it lives, to bind with the standard library (any socket type). A path
    the :mod:`socket` module takes is returned as is. ``ENAMETOOLONG`` where
    only :func:`bind`'s thread-cwd / at would reach."""
    trail = Trail()
    with _telling(trail):
        r = _calls.resolve_for_bind(path, opts=_opts(options), trail=trail)
    return ForBind(r.path, r.spelling, r.route, r.base, tuple(trail.skipped), r.link.cleanup if r.link else _nothing)


def resolve_for_connect(path: StrOrBytesPath, options: Options | None = None) -> ForConnect:
    """A short spelling of ``path`` to connect (or send) to with the
    standard library, as often as needed, then :meth:`ForConnect.cleanup`.
    A path the :mod:`socket` module takes is returned as is.
    ``ENAMETOOLONG`` where B cannot be had or the spelling does not fit
    (:func:`connect` may still reach it)."""
    trail = Trail()
    with _telling(trail):
        r = _calls.resolve_for_connect(path, opts=_opts(options), trail=trail)
    return ForConnect(r.path, r.route, r.base, tuple(trail.skipped), r.link.cleanup if r.link else _nothing)


def sweep() -> None:
    """Remove, from B of every automatic place, each D whose process has
    ended (its lock is gone). Only when called; another live process's D is
    never removed."""
    _calls.sweep(Env.current())
