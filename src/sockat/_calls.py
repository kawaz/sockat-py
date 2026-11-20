"""The calls the public modules are made of: a path given by the caller is
read once, a short one goes to the ``socket`` module as is (nothing read
from the environment), a long one through the routes (:mod:`._routes`)."""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from ._place import Env, LinkFile, sweep
from ._routes import (
    LINK_DIR,
    LINK_FILE,
    STANDARD,
    Bases,
    BindDone,
    Call,
    StrOrBytesPath,
    Target,
    Through,
    _Unusable,
    bind_long,
    bind_standard,
    check_no_nul,
    connect_long,
    enametoolong,
    link_dir_proc,
    link_dir_real,
    link_file_proc,
    link_file_real,
    look_up,
)
from ._socket_file import Link, Recorder, SocketFile
from ._sockets import PathSocket
from ._spelling import CwdRelative, Original, Spelling, Trail
from ._sys import DIRECT_MAX, IS_LINUX, Op

__all__ = ["StrOrBytesPath", "sweep"]


@dataclass(frozen=True)
class Opts:
    """``Options`` as the routes take them."""

    usable_cross_uid: bool = False
    base: bytes | None = None


PLAIN = Opts()


def _read(path: StrOrBytesPath) -> tuple[str | bytes, bytes, bool]:
    """The path read once (a path-like object may answer differently each
    time), its bytes, and whether it is a Linux abstract address (leading
    NUL, which goes to the ``socket`` module whatever its length). A NUL
    anywhere else is an argument error whatever the length."""
    spelled = os.fspath(path)
    raw = os.fsencode(spelled)
    abstract = IS_LINUX and raw.startswith(b"\0")
    if not abstract:
        check_no_nul(raw)
    return spelled, raw, abstract


def bind(
    path: StrOrBytesPath,
    *,
    type: int = socket.SOCK_STREAM,
    opts: Opts = PLAIN,
    remember: bool = False,
    trail: Trail | None = None,
    env: Env | None = None,
) -> tuple[BindDone, SocketFile | None]:
    """Bind (and, for a stream, listen) a fresh socket at ``path``. With
    ``remember`` (``sockat.ex``), the socket file is remembered right after
    the bind and a :class:`SocketFile` returned."""
    trail = trail if trail is not None else Trail()
    spelled, raw, abstract = _read(path)
    recorder = Recorder(raw) if remember else None
    call = Call(Op.BIND, type, recorder=recorder)
    if abstract or len(raw) <= DIRECT_MAX:
        trail.attempt(STANDARD)
        done = bind_standard(call, spelled, raw)
    else:
        env = env if env is not None else Env.current()
        t = look_up(raw)
        done = bind_long(call, t, env, Bases(env, t, opts.base, opts.usable_cross_uid), trail)
        if isinstance(done.sock, PathSocket):
            done.sock.report_path(os.fsdecode(raw), listening=True)
    if recorder is None:
        return done, None
    return done, SocketFile(recorder.file, done.link)


def connect(
    path: StrOrBytesPath,
    *,
    call: Call,
    opts: Opts = PLAIN,
    trail: Trail | None = None,
    env: Env | None = None,
    order: tuple[str, ...] | None = None,
) -> tuple[socket.socket, str]:
    """Connect ``call``'s socket (a fresh one, or the caller's datagram
    socket) to ``path``."""
    trail = trail if trail is not None else Trail()
    spelled, raw, abstract = _read(path)
    if abstract or len(raw) <= DIRECT_MAX:
        trail.attempt(STANDARD)
        return call.plain(spelled, standard=True), STANDARD
    env = env if env is not None else Env.current()
    t = look_up(raw)
    sock, route = connect_long(call, t, env, Bases(env, t, opts.base, opts.usable_cross_uid), trail, order)
    if isinstance(sock, PathSocket):
        sock.report_path(os.fsdecode(raw), listening=False)
    return sock, route


@dataclass(frozen=True)
class Resolved:
    """A resolve's outcome: the spelling (as the path was given, ``str`` or
    ``bytes``), the route, B's diagnostic name, the E to clean up."""

    path: str | bytes
    spelling: Spelling
    route: str
    base: str | None
    link: Link | None


def _as_given(spelled: str | bytes, spelling: bytes) -> str | bytes:
    return os.fsdecode(spelling) if isinstance(spelled, str) else spelling


def _short(spelled: str | bytes, raw: bytes) -> Resolved:
    spelling: Spelling = Original() if raw.startswith(b"/") or raw.startswith(b"\0") else CwdRelative()
    return Resolved(spelled, spelling, STANDARD, None, None)


def resolve_for_bind(path: StrOrBytesPath, *, opts: Opts, trail: Trail, env: Env | None = None) -> Resolved:
    """The SPEC's ``resolve_for_bind``: link-dir (procB, then B's real path),
    then link-file where a bind was measured to follow the symlink;
    ``ENAMETOOLONG`` past them."""
    spelled, raw, abstract = _read(path)
    if abstract or len(raw) <= DIRECT_MAX:
        trail.attempt(STANDARD)
        return _short(spelled, raw)
    env = env if env is not None else Env.current()
    t = look_up(raw)
    bases = Bases(env, t, opts.base, opts.usable_cross_uid)
    trail.skip(STANDARD, "path too long")
    makers: list[tuple[str, Maker]] = [(LINK_DIR, link_dir_proc), (LINK_DIR, link_dir_real)]
    if env.config.link_file is LinkFile.FOLLOWS:
        makers.append((LINK_FILE, link_file_real))
    return _resolve(spelled, makers, env, bases, trail)


def resolve_for_connect(path: StrOrBytesPath, *, opts: Opts, trail: Trail, env: Env | None = None) -> Resolved:
    """The SPEC's ``resolve_for_connect``: link-file (procB, then B's real
    path); ``ENAMETOOLONG`` past it."""
    spelled, raw, abstract = _read(path)
    if abstract or len(raw) <= DIRECT_MAX:
        trail.attempt(STANDARD)
        return _short(spelled, raw)
    env = env if env is not None else Env.current()
    t = look_up(raw)
    bases = Bases(env, t, opts.base, opts.usable_cross_uid)
    trail.skip(STANDARD, "path too long")
    makers: list[tuple[str, Maker]] = [(LINK_FILE, link_file_proc), (LINK_FILE, link_file_real)]
    return _resolve(spelled, makers, env, bases, trail)


Maker: TypeAlias = Callable[[Env, Target, Bases], Through]


def _resolve(spelled: str | bytes, makers: list[tuple[str, Maker]], env: Env, bases: Bases, trail: Trail) -> Resolved:
    for name, make in makers:
        try:
            through: Through = make(env, bases.target, bases)
        except _Unusable as u:
            trail.skip(name, u.why)
            continue
        trail.attempt(name)
        return Resolved(_as_given(spelled, through.spelling), through.linked, name, through.base, through.link)
    raise enametoolong()
