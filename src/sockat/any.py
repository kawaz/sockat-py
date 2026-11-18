"""Reach a socket path even by losing something (the sockat SPEC's any
operations), and say what was lost.

``import sockat`` does not import this module: using it is a choice, made by
importing it::

    from sockat.any import Loss, bind_any

    listener, losses, route, skipped = bind_any("/a/very/deep/directory/.../server.sock")
    if Loss.RECREATABLE_SPELLING in losses:
        ...  # remove the socket file by the path given, never by the address
    server = await asyncio.start_unix_server(handle, sock=listener)

:func:`bind_any` binds as :func:`sockat.bind` does, and where every route of
:func:`sockat.bind` is unusable (it would raise ``ENAMETOOLONG``), goes on to
a route :func:`sockat.bind` never takes: a short temporary symlink to the
socket's directory in a place for temporary files.

The SPEC's ``connect_any`` is not here: what it adds is a fork route, which
this implementation does not have, so it would be :func:`sockat.connect`.
"""

from __future__ import annotations

import socket
from typing import NamedTuple

from ._route import BIND_ANY, Loss, Route, StrOrBytesPath, Trail, reach

__all__ = ["AnyBinding", "Loss", "Skipped", "bind_any"]


class Skipped(NamedTuple):
    """A route :func:`bind_any` tried and could not use. For diagnosis only:
    neither the names nor the reasons are promised (the routes may be added
    to or changed)."""

    name: str
    """The route's name in the sockat SPEC (``"thread-cwd"``, ``"proc"``,
    ...)."""
    reason: str
    """Why it could not be used, a short phrase (``"seccomp filter"``,
    ``"/proc unusable"``, ``"spelling too long"``, ...)."""


class AnyBinding(NamedTuple):
    """What :func:`bind_any` returns:
    ``listener, losses, route, skipped = bind_any(path)``."""

    listener: socket.socket
    """The listening socket, as :func:`sockat.bind` returns it (for a long
    path, one that reports the path given as its address)."""
    losses: tuple[Loss, ...]
    """What the route that carried the bind lost compared with the standard,
    each once (empty when nothing was lost). Branch on this. The names are
    promised and only ever added to: read one you do not know as "something
    was lost"."""
    route: str
    """The name of the route that carried the bind, as the sockat SPEC names
    it (``"standard"``, ``"thread-cwd"``, ``"symlink"``, ...). For diagnosis
    only: not promised."""
    skipped: tuple[Skipped, ...]
    """The routes tried before :attr:`route` and found unusable, in the order
    tried (every route tried before the one that carried the bind could not
    be used: one whose bind fails raises instead). For diagnosis only: not
    promised."""


def bind_any(path: StrOrBytesPath) -> AnyBinding:
    """Bind (and listen) an ``AF_UNIX`` stream socket at ``path`` as
    :func:`sockat.bind` does, and where :func:`sockat.bind` would raise
    ``ENAMETOOLONG`` because none of its routes can be used, go on through a
    temporary symlink: ``<place>/sockat.XXXXXXXXXX/<file name>``, the symlink
    pointing to the realpath of the socket's directory and removed right
    after the bind.

    The places are ``$XDG_RUNTIME_DIR`` and ``$TMPDIR`` (when set to an
    absolute path), then ``/tmp``. Every one that only you can write to
    (owned by you, no write permission for group and others) is tried before
    any shared one (sticky, owned by root or you); other places are not
    used. A place where the spelling does not fit ``sun_path`` or the symlink
    cannot be created is passed over; nothing is left in any place.

    :attr:`AnyBinding.losses` (no order of size between the names):

    * nothing for an absolute path the :mod:`socket` module takes;
    * :attr:`Loss.CWD_RELATIVE` for a relative path it takes;
    * :attr:`Loss.RESPELLED` for every other route of :func:`sockat.bind`,
      and the symlink in a place only you can write to;
    * :attr:`Loss.RESPELLED` and :attr:`Loss.RECREATABLE_SPELLING` for the
      symlink in a shared place.

    :attr:`AnyBinding.route` and :attr:`AnyBinding.skipped` are for
    diagnosis and their contents are not promised.

    Errors are those of :func:`sockat.bind` (an :class:`OSError` keeps its
    type and ``errno``; ``ENAMETOOLONG`` when no place carries the bind
    either), each with two attributes added: ``skipped`` (as
    :attr:`AnyBinding.skipped`, up to the failure) and ``route`` (the name
    of the route whose bind failed, or ``None`` when none got that far).
    """
    trail = Trail()
    try:
        sock, route, losses, skipped = reach(BIND_ANY, path, trail)
    except (OSError, ValueError) as e:
        # Design rationale: the error is the one sockat.bind raises, with
        # attributes added rather than wrapped or subclassed, so that its
        # type (FileNotFoundError, ...) and errno stay what callers test.
        setattr(e, "skipped", _skipped(trail.skipped))  # noqa: B010
        setattr(e, "route", None if trail.trying is None else trail.trying.value)  # noqa: B010
        raise
    return AnyBinding(sock, losses, route.value, _skipped(skipped))


def _skipped(trail: list[tuple[Route, str]] | tuple[tuple[Route, str], ...]) -> tuple[Skipped, ...]:
    return tuple(Skipped(route.value, why) for route, why in trail)
