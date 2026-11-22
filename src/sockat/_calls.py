"""The calls the public modules are made of: a path given by the caller is
read once, a short one goes to the ``socket`` module as is (nothing read
from the environment), a long one through the routes (:mod:`._routes`)."""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from . import _absolute
from ._path import split
from ._place import Env, LinkFile, check_given, sweep, sweep_given
from ._routes import (
    LINK_DIR,
    LINK_FILE,
    STANDARD,
    Bases,
    BindDone,
    Call,
    RealPathTooLong,
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
from ._socket_file import Link, Recorder, SocketFile, cwd_id
from ._sockets import PathSocket
from ._spelling import CwdRelative, Original, Spelling, Trail
from ._sys import DIRECT_MAX, IS_LINUX, Op

__all__ = ["StrOrBytesPath", "sweep", "sweep_given"]


@dataclass(frozen=True)
class Opts:
    """``Options`` as the routes take them."""

    usable_cross_uid: bool = False
    base: bytes | None = None


PLAIN = Opts()


def _read(path: StrOrBytesPath) -> tuple[str | bytes, bytes, bool]:
    """The path read once (a path-like object may answer differently each
    time), its bytes, and whether it is a Linux abstract address (leading
    NUL, which goes to the ``socket`` module whatever its length). An empty
    path, and a NUL anywhere else, are argument errors whatever the
    length."""
    spelled = os.fspath(path)
    raw = os.fsencode(spelled)
    if not raw:
        raise ValueError("socket path is empty")
    abstract = IS_LINUX and raw.startswith(b"\0")
    if not abstract:
        check_no_nul(raw)
    return spelled, raw, abstract


def _long(raw: bytes, env: Env, opts: Opts, *, cross_uid: bool) -> Bases:
    """The steps of a long path before any route: the socket's directory
    looked up, then ``Options.base`` checked (making nothing), so that
    either fails with no route tried; and the B the routes may use."""
    return _bases(look_up(raw), env, opts, cross_uid=cross_uid)


def _bases(t: Target, env: Env, opts: Opts, *, cross_uid: bool) -> Bases:
    """``Options.base`` checked (making nothing) for the path looked up as
    ``t``, and the B the routes may use."""
    if opts.base is not None:
        check_given(opts.base, env.config.uid, cross_uid=cross_uid, held=env.held_given(opts.base))
    return Bases(env, t, opts.base, cross_uid)


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
    the bind and a :class:`SocketFile` returned.

    A short absolute path (and an abstract name) goes to the ``socket``
    module as it is. A relative path is made absolute first (SPEC
    "相対パスを絶対パスにする"): an absolute candidate that fits goes to the
    ``socket`` module; otherwise the routes; and when no route can be used,
    a short relative path goes to the ``socket`` module as it is
    (``CwdRelative``). A long absolute path whose real candidate fits goes
    to the ``socket`` module too."""
    trail = trail if trail is not None else Trail()
    spelled, raw, abstract = _read(path)
    recorder = Recorder(raw) if remember else None
    call = Call(Op.BIND, type, recorder=recorder)
    done = _bind(call, spelled, raw, abstract, opts, remember, trail, env)
    if recorder is None:
        return done, None
    return done, SocketFile(recorder.file, done.link)


def _bind(
    call: Call,
    spelled: str | bytes,
    raw: bytes,
    abstract: bool,
    opts: Opts,
    remember: bool,
    trail: Trail,
    env: Env | None,
) -> BindDone:
    relative = not abstract and not raw.startswith(b"/")
    if abstract or (not relative and len(raw) <= DIRECT_MAX) or (relative and _short_without_name(raw)):
        trail.attempt(STANDARD)
        return bind_standard(call, spelled, raw)
    if relative:
        candidate = _absolute.fitting(raw)
        if candidate is not None:
            trail.attempt(STANDARD)
            return bind_standard(call, _as_given(spelled, candidate), candidate)
    env = env if env is not None else Env.current()
    try:
        t = look_up(raw, absolutizing=True)
    except RealPathTooLong:
        return _bind_as_given(call, spelled, raw, trail)
    if not relative:
        real = _join(t.real_dir, t.name)
        if len(real) <= DIRECT_MAX:
            trail.attempt(STANDARD)
            done = bind_standard(call, _as_given(spelled, real), real, plain_type=False)
            _report(done.sock, raw)
            return done
    bases = _bases(t, env, opts, cross_uid=opts.usable_cross_uid)
    # The default bind (no SocketFile to clean up with) uses link-dir's E
    # again per socket directory.
    bases.share_e = not remember
    done_long = bind_long(call, t, env, bases, trail)
    if done_long is None:
        if relative:
            return _bind_as_given(call, spelled, raw, trail)
        raise enametoolong()
    # The address reported: the path given, or for a relative path the real
    # candidate's form (SPEC: the absolute form, not the relative string).
    _report(done_long.sock, _join(t.real_dir, t.name) if relative else raw)
    return done_long


def _bind_as_given(call: Call, spelled: str | bytes, raw: bytes, trail: Trail) -> BindDone:
    """No route could be used for the relative path: a short one goes to
    the ``socket`` module as it is (``CwdRelative``; the routes found
    unusable stay in ``skipped``, ``standard`` not among them), a long one
    is ``ENAMETOOLONG``."""
    if len(raw) > DIRECT_MAX:
        raise enametoolong()
    trail.attempt(STANDARD)
    return bind_standard(call, spelled, raw)


def _short_without_name(raw: bytes) -> bool:
    """A short relative path that does not end with a file name (``dir/``,
    ``..``): given to the ``socket`` module as it is, not made absolute (no
    candidate without a file name; the standard's own error)."""
    if len(raw) > DIRECT_MAX:
        return False
    try:
        split(raw)
    except ValueError:
        return True
    return False


def _report(sock: socket.socket, path: bytes) -> None:
    if isinstance(sock, PathSocket):
        sock.report_path(os.fsdecode(path), listening=True)


def _join(a: bytes, b: bytes) -> bytes:
    return a + b if a.endswith(b"/") else a + b"/" + b


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
        sock = call.plain(spelled, standard=True)
        if isinstance(sock, PathSocket):
            # The caller's datagram socket, connected again to a short path:
            # its peer is the kernel's again.
            sock.report_path(None, listening=False)
        return sock, STANDARD
    env = env if env is not None else Env.current()
    # A connect's spelling stays nowhere: usable_cross_uid is not looked at.
    bases = _long(raw, env, opts, cross_uid=False)
    sock, route = connect_long(call, bases.target, env, bases, trail, order)
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
    raw: bytes
    """The spelling's bytes."""
    cwd: tuple[int, int] | None
    """For a relative spelling, the cwd's ``(st_dev, st_ino)`` when it was
    resolved (``ForBind.socket_file`` compares it)."""


def _as_given(spelled: str | bytes, spelling: bytes) -> str | bytes:
    return os.fsdecode(spelling) if isinstance(spelled, str) else spelling


def resolve_for_bind(path: StrOrBytesPath, *, opts: Opts, trail: Trail, env: Env | None = None) -> Resolved:
    """The SPEC's ``resolve_for_bind``: link-dir (procB, then B's real path),
    then link-file where a bind was measured to follow the symlink;
    ``ENAMETOOLONG`` past them. A relative path is made absolute first, as
    for a bind."""

    def makers(env: Env) -> list[tuple[str, Maker]]:
        out: list[tuple[str, Maker]] = [(LINK_DIR, link_dir_proc), (LINK_DIR, link_dir_real)]
        if env.config.link_file is LinkFile.FOLLOWS:
            out.append((LINK_FILE, link_file_real))
        return out

    return _resolve(path, makers, opts, opts.usable_cross_uid, trail, env)


def resolve_for_connect(path: StrOrBytesPath, *, opts: Opts, trail: Trail, env: Env | None = None) -> Resolved:
    """The SPEC's ``resolve_for_connect``: link-file (procB, then B's real
    path); ``ENAMETOOLONG`` past it. ``usable_cross_uid`` is not looked at
    (a connect's spelling stays nowhere). A relative path is made absolute
    first (the spelling may be used after the cwd has changed)."""
    return _resolve(
        path, lambda env: [(LINK_FILE, link_file_proc), (LINK_FILE, link_file_real)], opts, False, trail, env
    )


Maker: TypeAlias = Callable[[Env, Target, Bases], Through]


def _resolve(
    path: StrOrBytesPath,
    makers: Callable[[Env], list[tuple[str, Maker]]],
    opts: Opts,
    cross_uid: bool,
    trail: Trail,
    env: Env | None,
) -> Resolved:
    """The steps of a bind, without the bind (SPEC "resolve"): a short
    absolute path as it is, an absolute candidate that fits, a route's
    spelling, and for a relative path no route could be used for, the
    relative path as it is (``CwdRelative``) when it is short."""
    spelled, raw, abstract = _read(path)
    relative = not abstract and not raw.startswith(b"/")
    cwd = cwd_id() if relative else None
    if abstract or (not relative and len(raw) <= DIRECT_MAX) or (relative and _short_without_name(raw)):
        trail.attempt(STANDARD)
        return _standard(spelled, raw, cwd)
    if relative:
        candidate = _absolute.fitting(raw)
        if candidate is not None:
            trail.attempt(STANDARD)
            return _standard(_as_given(spelled, candidate), candidate, None)
    env = env if env is not None else Env.current()
    try:
        t = look_up(raw, absolutizing=True)
    except RealPathTooLong:
        return _resolve_as_given(spelled, raw, cwd, trail)
    if not relative:
        real = _join(t.real_dir, t.name)
        if len(real) <= DIRECT_MAX:
            trail.attempt(STANDARD)
            return _standard(_as_given(spelled, real), real, None)
    bases = _bases(t, env, opts, cross_uid=cross_uid)
    trail.skip(STANDARD, "path too long")
    for name, make in makers(env):
        try:
            through: Through = make(env, t, bases)
        except _Unusable as u:
            trail.skip(name, u.why)
            continue
        except OSError:
            trail.forget(name)
            raise
        trail.attempt(name)
        return Resolved(
            _as_given(spelled, through.spelling),
            through.linked,
            name,
            through.base,
            through.link,
            through.spelling,
            None,
        )
    if relative:
        return _resolve_as_given(spelled, raw, cwd, trail)
    raise enametoolong()


def _standard(spelled: str | bytes, raw: bytes, cwd: tuple[int, int] | None) -> Resolved:
    """A spelling given back as it is: an absolute one (``Original``), or a
    relative one (``CwdRelative``, with the cwd it was given against)."""
    if raw.startswith(b"/") or raw.startswith(b"\0"):
        return Resolved(spelled, Original(), STANDARD, None, None, raw, None)
    return Resolved(spelled, CwdRelative(), STANDARD, None, None, raw, cwd)


def _resolve_as_given(spelled: str | bytes, raw: bytes, cwd: tuple[int, int] | None, trail: Trail) -> Resolved:
    """No route could be used for the relative path: a short one is given
    back as it is (``CwdRelative``), a long one is ``ENAMETOOLONG``."""
    if len(raw) > DIRECT_MAX:
        raise enametoolong()
    trail.attempt(STANDARD)
    return _standard(spelled, raw, cwd)
