"""The calls of :mod:`sockat` with what they went through (the sockat SPEC's
``_ex``): the route taken, what it lost compared with the standard, the
routes skipped, and for a bind ``discard``, which removes the socket file
the bind made and nothing else. Each can also be asked to use the last
resort, a route that reaches the path even by losing something.

``import sockat`` does not import this module: using it is a choice, made by
importing it. Call it through the module, so that it is not mistaken for
the plain calls of :mod:`sockat`::

    import sockat.ex

    listener, discard, losses, route, skipped = sockat.ex.bind(path)
    if sockat.ex.Loss.RECREATABLE_SPELLING in losses:
        ...  # remove the socket file by the path given (discard), never by the address
    server = await asyncio.start_unix_server(handle, sock=listener)
    ...
    discard()

    sock, losses, route, skipped = sockat.ex.connect(path)

Without ``last_resort`` each call takes the routes of its plain counterpart
in the same order, and reaches what that reaches. With ``last_resort=True``:

* :func:`bind` goes on, where every route of :func:`sockat.bind` is unusable
  (it would raise ``ENAMETOOLONG``), to a route :func:`sockat.bind` never
  takes: a short temporary symlink to the socket's directory in a place for
  temporary files (symlink-dir);
* :func:`connect` / :func:`connect_nonblocking` would go on to the SPEC's
  fork route, which this implementation does not have: it is noted as
  skipped, and the call reaches what the plain one reaches.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from typing import NamedTuple

from . import _socket_file
from ._route import Loss, Request, Route, StrOrBytesPath, Trail, reach
from ._sys import Op

__all__ = ["Bound", "Connected", "Loss", "Skipped", "bind", "connect", "connect_nonblocking"]


class Skipped(NamedTuple):
    """A route tried and found unusable. For diagnosis only: neither the
    names nor the reasons are promised (the routes may be added to or
    changed)."""

    name: str
    """The route's name in the sockat SPEC (``"symlink-file"``,
    ``"thread-cwd"``, ``"proc"``, ...)."""
    reason: str
    """Why it could not be used, a short phrase (``"seccomp filter"``,
    ``"/proc unusable"``, ``"spelling too long"``, ...)."""


class Bound(NamedTuple):
    """What :func:`bind` returns:
    ``listener, discard, losses, route, skipped = sockat.ex.bind(path)``."""

    listener: socket.socket
    """The listening socket, as :func:`sockat.bind` returns it (for a long
    path, one that reports the path given as its address)."""
    discard: Callable[[], None]
    """Remove the socket file the bind made (see :func:`bind`)."""
    losses: tuple[Loss, ...]
    """What the route taken lost compared with the standard, each once
    (empty when nothing was lost). Branch on this. The names are promised
    and only ever added to: read one you do not know as "something was
    lost"."""
    route: str
    """The name of the route taken, as the sockat SPEC names it
    (``"standard"``, ``"symlink-file"``, ``"thread-cwd"``,
    ``"symlink-dir"``, ...). For diagnosis only: not promised."""
    skipped: tuple[Skipped, ...]
    """The routes tried before :attr:`route` and found unusable, in the order
    tried (every route tried before the one taken could not be used: one
    whose bind fails raises instead). For diagnosis only: not promised."""


class Connected(NamedTuple):
    """What :func:`connect` / :func:`connect_nonblocking` return:
    ``sock, losses, route, skipped = sockat.ex.connect(path)``."""

    sock: socket.socket
    """The connected socket, as :func:`sockat.connect` /
    :func:`sockat.connect_nonblocking` return it."""
    losses: tuple[Loss, ...]
    """As :attr:`Bound.losses`. A connect's spelling stays nowhere, so the
    routes here lose nothing: empty."""
    route: str
    """As :attr:`Bound.route`."""
    skipped: tuple[Skipped, ...]
    """As :attr:`Bound.skipped`."""


def bind(path: StrOrBytesPath, *, last_resort: bool = False) -> Bound:
    """:func:`sockat.bind`, with what it went through and ``discard``.

    ``discard()`` removes the socket file the bind made, and nothing else:
    right after the bind goes through, before the listener is returned, the
    realpath of the socket file and its ``(st_dev, st_ino)`` are remembered
    (when that fails, the listener is closed and the error raised), and
    ``discard()`` removes the file at that realpath only while it is still
    that file. When it is gone, or has been replaced by another file, it
    does nothing. It may be called any number of times. It does not close
    the listener (close it yourself, or let asyncio close it when it serves
    it). Errors other than the file being gone are raised as
    :class:`OSError`.

    With ``last_resort``, where :func:`sockat.bind` would raise
    ``ENAMETOOLONG`` because none of its routes can be used, the bind goes on
    through a temporary symlink to the socket's directory (symlink-dir):
    ``<place>/sockat.XXXXXXXXXX/<file name>``, the symlink pointing to the
    realpath of the socket's directory and removed right after the bind. The
    places are ``$XDG_RUNTIME_DIR`` and ``$TMPDIR`` (when set to an absolute
    path), then ``/tmp``. Every one that only you can write to (owned by
    you, no write permission for group and others) is tried before any
    shared one (sticky, owned by root or you); other places are not used. A
    place where the spelling does not fit ``sun_path`` or the symlink cannot
    be created is passed over; nothing is left in any place.

    :attr:`Bound.losses` (no order of size between the names):

    * nothing for an absolute path the :mod:`socket` module takes;
    * :attr:`Loss.CWD_RELATIVE` for a relative path it takes;
    * :attr:`Loss.RESPELLED` for every other route, symlink-dir in a place
      only you can write to included;
    * :attr:`Loss.RESPELLED` and :attr:`Loss.RECREATABLE_SPELLING` for
      symlink-dir in a shared place.

    Errors are those of :func:`sockat.bind` (an :class:`OSError` keeps its
    type and ``errno``; ``ENAMETOOLONG`` when no route reaches the path),
    each with two attributes added: ``skipped`` (as :attr:`Bound.skipped`,
    up to the failure) and ``route`` (the name of the route whose bind
    failed, or ``None`` when none got that far).
    """
    # The path is read once (os.fspath), and that one value is what is bound
    # and remembered: a path-like object may answer differently each time.
    spelled = os.fspath(path)
    trail = Trail()
    try:
        with _socket_file.recording(spelled) as recorder:
            sock, route, losses, skipped = reach(Request(Op.BIND, last_resort=last_resort), spelled, trail)
    except (OSError, ValueError) as e:
        _tell(e, trail)
        raise
    discard = recorder.discard
    if discard is None:  # every route records right after its bind
        sock.close()
        raise RuntimeError("the bind did not record the socket file it made")
    return Bound(sock, discard, losses, route.value, _skipped(skipped))


def connect(path: StrOrBytesPath, *, last_resort: bool = False) -> Connected:
    """:func:`sockat.connect`, with what it went through.

    ``last_resort`` would let the connect go on to the SPEC's fork route
    where every route of :func:`sockat.connect` is unusable. This
    implementation has no fork route: it is noted in :attr:`Connected.skipped`
    (and on the ``ENAMETOOLONG`` raised), and the call reaches what
    :func:`sockat.connect` reaches.

    Errors are those of :func:`sockat.connect`, with ``skipped`` and
    ``route`` added as for :func:`bind`."""
    return _connect(Request(Op.CONNECT, last_resort=last_resort), path)


def connect_nonblocking(path: StrOrBytesPath, *, last_resort: bool = False) -> Connected:
    """:func:`sockat.connect_nonblocking`, with what it went through, as
    :func:`connect`."""
    return _connect(Request(Op.CONNECT, nonblocking=True, last_resort=last_resort), path)


def _connect(req: Request, path: StrOrBytesPath) -> Connected:
    trail = Trail()
    try:
        sock, route, losses, skipped = reach(req, path, trail)
    except (OSError, ValueError) as e:
        _tell(e, trail)
        raise
    return Connected(sock, losses, route.value, _skipped(skipped))


def _tell(e: BaseException, trail: Trail) -> None:
    """Add ``skipped`` and ``route`` from ``trail`` to the error ``e``.

    Design rationale: the error is the one the plain call raises, with
    attributes added rather than wrapped or subclassed, so that its type
    (FileNotFoundError, ...) and errno stay what callers test."""
    setattr(e, "skipped", _skipped(trail.skipped))  # noqa: B010
    setattr(e, "route", None if trail.trying is None else trail.trying.value)  # noqa: B010


def _skipped(trail: list[tuple[Route, str]] | tuple[tuple[Route, str], ...]) -> tuple[Skipped, ...]:
    return tuple(Skipped(route.value, why) for route, why in trail)
