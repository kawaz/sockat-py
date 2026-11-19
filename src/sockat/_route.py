"""The order in which the ways of reaching a long socket path are tried (the
language-neutral SPEC of the hub kawaz/sockat, its DR-0012, and
docs/decisions/DR-0001). :func:`run` is the only entry point used by the
public API; the per-route functions are module-level so the tests can force
each route."""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import socket
import stat
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Literal, NamedTuple, TypeAlias

from . import _path, _socket_file
from ._sockets import PathSocket
from ._sys import (
    DIRECT_MAX,
    HAS_THREAD_CWD,
    IS_APPLE,
    IS_FREEBSD,
    IS_LINUX,
    WIDE_PATH_MAX,
    Op,
    enter_dir_in_this_thread,
    op,
    op_at,
    sockaddr,
    thread_cwd_possible,
)

StrOrBytesPath: TypeAlias = "str | bytes | os.PathLike[str] | os.PathLike[bytes]"


@dataclass(frozen=True)
class Request:
    """What to do at a path."""

    op: Op
    nonblocking: bool = False
    """Make the socket nonblocking before the call, on every route, and leave
    it so (``connect_nonblocking``). A full backlog then fails the connect at
    once: Linux answers ``EAGAIN``, macOS ``ECONNREFUSED``. A Unix-domain
    connect completes or fails within the call even on a nonblocking socket
    (neither Linux nor XNU answers ``EINPROGRESS``)."""
    at_any_cost: bool = False
    """Reach the path even by losing something (``bind_any``, the SPEC's any
    operations): where every route of the default bind is unusable (and it
    would answer ``ENAMETOOLONG``), the bind goes on to the symlink route
    (:func:`bind_symlink_route`)."""


BIND = Request(Op.BIND)
BIND_ANY = Request(Op.BIND, at_any_cost=True)
CONNECT = Request(Op.CONNECT)
CONNECT_NONBLOCKING = Request(Op.CONNECT, nonblocking=True)


class Route(Enum):
    """Which route a bind / connect went by, named as the SPEC names it (its
    "route names"; the value is that name). For diagnosis only: the routes
    may be added to or changed. Only ``bind_any`` shows it (the value)."""

    STANDARD = "standard"
    """The path itself, through the ``socket`` module."""
    THREAD_CWD = "thread-cwd"
    """``R/<name>`` from a throwaway thread whose own cwd is the private
    directory D (Linux, Apple)."""
    AT = "at"
    """``bindat(2)`` / ``connectat(2)`` of ``R/<name>`` on the fd of D
    (FreeBSD)."""
    PROC = "proc"
    """``/proc/self/fd/<fd of D>/R/<name>``, through the ``socket`` module
    (Linux)."""
    FORK = "fork"
    """The SPEC's fork route of a bind, which this implementation does not
    have (DR-0001): only ever noted as skipped."""
    THREAD_CWD_SOCKDIR = "thread-cwd-sockdir"
    """A bind's form without D, where D could be made nowhere:
    ``.sockat.XXXXXXXXXX/<name>`` (R in the socket's directory) from a
    throwaway thread whose own cwd is the socket's directory (Linux,
    Apple)."""
    AT_SOCKDIR = "at-sockdir"
    """A bind's form without D: ``bindat(2)`` of
    ``.sockat.XXXXXXXXXX/<name>`` on the fd of the socket's directory
    (FreeBSD)."""
    PROC_SOCKDIR = "proc-sockdir"
    """A bind's form without D through /proc:
    ``/proc/self/fd/<fd of the socket's directory>/.sockat.XXXXXXXXXX/<name>``,
    through the ``socket`` module (Linux)."""
    FORK_SOCKDIR = "fork-sockdir"
    """The SPEC's fork route in the form without D, which this
    implementation does not have: only ever noted as skipped."""
    PROC_DIRECT = "proc-direct"
    """A connect's fallback: ``/proc/self/fd/<fd of the socket's
    directory>/<name>``, through the ``socket`` module (Linux)."""
    PROC_HELD = "proc-held"
    """A connect's fallback: ``/proc/self/fd/<fd of D>/s``, ``s`` a symlink
    to the socket file (Linux)."""
    RELATIVE = "relative"
    """A connect's fallback: the path spelled relative to the cwd, through
    the ``socket`` module."""
    WIDENED = "widened"
    """A connect's fallback: the path itself, or else its relative spelling,
    in a widened ``sockaddr_un`` (Apple)."""
    SYMLINK = "symlink"
    """A short temporary symlink in a place for it: a connect's fallback
    through ``<place>/sockat.XXXXXXXXXX`` pointing to the socket file (in a
    widened ``sockaddr_un`` on Apple when the spelling needs it), or the
    last route of ``bind_any`` through ``<place>/sockat.XXXXXXXXXX/<name>``
    pointing to the socket's directory."""
    AT_DIRECT = "at-direct"
    """A connect's fallback: ``connectat(2)`` of the file name on the fd of
    the socket's directory (FreeBSD)."""


class Loss(StrEnum):
    """One thing a route loses compared with the standard (the SPEC's "what
    is lost"); a call reports every one its route loses, in no order of
    size. The string of a value (``str()``, ``.value``) is the SPEC's name.

    The names are promised and only ever added to: read a name you do not
    know as "something was lost"."""

    RESPELLED = "respelled"
    """The address the kernel reports is not the path given (sockat's
    listener reports the path all the same), so whatever removes the socket
    file by the kernel's address does not remove it: remove it by the path
    given. The spelling's random part is gone, so it names nothing as a
    rule, but the spelling may be seen by other users (Linux's
    ``/proc/net/unix``): where the process's cwd is a place others can write
    to, one of them can recreate it, and removing a file by it may then
    remove a file of the same name in a directory of their choosing."""
    FORKED = "forked"
    """The side effects of a fork (the SPEC's fork routes). This
    implementation has no fork route, so it never reports this."""
    CWD_RELATIVE = "cwd-relative"
    """The spelling the kernel keeps is relative to the cwd (a relative path
    went to the ``socket`` module as it is): once the cwd changes, removing
    or looking up a file by it reaches another place. A danger of the
    standard's own rather than one a route brings, reported all the same.
    Remove the socket file by the path given made absolute."""
    RECREATABLE_SPELLING = "recreatable-spelling"
    """The spelling the kernel reports goes through a shared place (a sticky
    one such as ``/tmp``): its name is random, but other users can see it
    once bound (Linux's ``/proc/net/unix``) and recreate it, and removing a
    file by it may then remove a file of the same name in a directory of
    their choosing. Remove the socket file by the path given."""


class SocketDir(Enum):
    """The socket's own directory as a place for D (the last of
    :attr:`Config.own_dir_places`)."""

    SOCKET_DIR = "socket-dir"


SOCKET_DIR = SocketDir.SOCKET_DIR

OwnDirPlace: TypeAlias = "bytes | Literal[SocketDir.SOCKET_DIR]"


def _enametoolong() -> OSError:
    return OSError(errno.ENAMETOOLONG, os.strerror(errno.ENAMETOOLONG))


def _join(a: bytes, b: bytes) -> bytes:
    return a + b if a.endswith(b"/") else a + b"/" + b


def _same_spelling(a: bytes, b: bytes) -> bool:
    """Whether two places are the same directory written two ways that the
    kernel walks alike (``/tmp``, ``/tmp/``, ``//tmp/.``)."""
    return a.startswith(b"/") == b.startswith(b"/") and _path.components(a) == _path.components(b)


@dataclass(frozen=True)
class Config:
    """What the route order reads from the environment, made explicit so the
    tests can vary it without touching the process environment."""

    own_dir_places: tuple[OwnDirPlace, ...]
    """Where the private directory D may be made, in the order tried:
    ``${TMPDIR:-/tmp}`` (a relative ``TMPDIR`` included), ``/tmp`` unless it
    is the same place, then the socket's own directory (:data:`SOCKET_DIR`).
    The tests leave places out to stand in for places where D cannot be
    made."""
    link_places: tuple[bytes, ...]
    """Where a connect's temporary symlink may go, in the order tried:
    ``TMPDIR`` when it is set to an absolute path, then ``/tmp``."""
    bind_link_places: tuple[bytes, ...] = ()
    """Where ``bind_any``'s temporary symlink may go, the candidates in the
    order given: ``XDG_RUNTIME_DIR`` and ``TMPDIR`` when they are set to an
    absolute path, then ``/tmp``, each spelling once. Which of them are
    tried first is decided by what they are (:func:`bind_symlink_route`)."""
    wide: bool = IS_APPLE
    """Whether a connect tries the widened ``sockaddr_un`` at all (Apple).
    The tests turn it off to stand in for a kernel that refuses the widened
    form."""
    proc_self_fd: bytes = b"/proc/self/fd"
    """Where the kernel lists the fds of the process (Linux). The tests point
    it elsewhere to stand in for a system without /proc."""
    thread_cwd: bool = HAS_THREAD_CWD
    """Whether the routes through D try a thread's own cwd at all (Linux,
    Apple). The tests turn it off to stand in for a system where it cannot be
    used."""

    @classmethod
    def from_env(cls) -> Config:
        return cls.from_tmpdir(os.environb.get(b"TMPDIR"), os.environb.get(b"XDG_RUNTIME_DIR"))

    @classmethod
    def from_tmpdir(cls, tmpdir: bytes | None, xdg_runtime_dir: bytes | None = None) -> Config:
        """The configuration for a ``TMPDIR`` of ``tmpdir`` (and an
        ``XDG_RUNTIME_DIR`` of ``xdg_runtime_dir``).

        Places for D: ``${TMPDIR:-/tmp}`` (unset or empty: ``/tmp``; a
        relative one is opened against the cwd once and held by its fd),
        ``/tmp`` unless the first is ``/tmp`` itself, then the socket's
        directory.

        Places for a connect's symlink: ``TMPDIR`` when it is absolute and
        not ``/tmp`` itself (a relative one would make the link's place
        depend on the cwd), then ``/tmp``.

        Places for ``bind_any``'s symlink: ``XDG_RUNTIME_DIR``, then
        ``TMPDIR``, when absolute, then ``/tmp``; a place spelled as an
        earlier one is left out."""
        first = tmpdir or b"/tmp"
        own: list[OwnDirPlace] = [first]
        if not _same_spelling(first, b"/tmp"):
            own.append(b"/tmp")
        own.append(SOCKET_DIR)
        return cls(
            own_dir_places=tuple(own),
            link_places=_link_places(tmpdir),
            bind_link_places=_link_places(xdg_runtime_dir, tmpdir),
        )


def _link_places(*env: bytes | None) -> tuple[bytes, ...]:
    """The places for a temporary symlink: each of ``env`` that is an
    absolute path (a relative one would make the link's place depend on the
    cwd), in order, then ``/tmp``, leaving out one spelled as ``/tmp`` or as
    an earlier one."""
    places: list[bytes] = []
    for place in env:
        if place and place.startswith(b"/") and not any(_same_spelling(place, p) for p in (*places, b"/tmp")):
            places.append(place)
    places.append(b"/tmp")
    return tuple(places)


class Trail:
    """The routes tried and found unusable, in order, each with why (a short
    phrase, for diagnosis), and the route being tried now: the one whose
    bind / connect failed when the call raises."""

    def __init__(self) -> None:
        self.skipped: list[tuple[Route, str]] = []
        self.trying: Route | None = None

    def skip(self, route: Route, why: str) -> None:
        """Note ``route`` as unusable for ``why``."""
        self.skipped.append((route, why))
        self.trying = None

    def attempt(self, route: Route) -> None:
        """Note that ``route`` is being tried (its bind / connect runs)."""
        self.trying = route


class Reached(NamedTuple):
    """How a bind / connect went through: the socket, the route it went by,
    what that route lost (the bind's; a connect loses nothing on these
    routes), and the routes tried before it that could not be used, in
    order, each with why. The connect's fallbacks are not noted (there is no
    ``connect_any`` here to report them)."""

    sock: socket.socket
    route: Route
    losses: tuple[Loss, ...]
    skipped: tuple[tuple[Route, str], ...]


def run(req: Request, path: StrOrBytesPath) -> tuple[socket.socket, Route]:
    """:func:`reach`'s socket and route."""
    sock, route, _, _ = reach(req, path, Trail())
    return sock, route


def reach(req: Request, path: StrOrBytesPath, trail: Trail) -> Reached:
    """Bind (and listen) or connect a stream socket to ``path``, trying the
    routes in order, noting them in ``trail`` (which tells, when this raises,
    what was skipped and which route failed).

    A path the ``socket`` module takes (at most :data:`DIRECT_MAX` bytes) goes
    to it as is, before anything else is looked at (the environment included).

    A NUL byte makes the path an argument error whatever its length (the
    ``socket`` module would silently cut the path there), except a leading NUL
    on Linux / Android: that is an address in the abstract namespace, not a
    pathname, and goes to the ``socket`` module as is whatever its length.
    """
    spelled = os.fspath(path)
    raw = os.fsencode(spelled)
    abstract = IS_LINUX and raw.startswith(b"\0")
    if not abstract:
        check_no_nul(raw)
    if abstract or len(raw) <= DIRECT_MAX:
        trail.attempt(Route.STANDARD)
        sock = plain_op(req, spelled, standard=True)
        relative = req.op is Op.BIND and not abstract and not raw.startswith(b"/")
        return Reached(sock, Route.STANDARD, (Loss.CWD_RELATIVE,) if relative else (), ())
    return reach_long(req, raw, Config.from_env(), trail)


def check_no_nul(path: bytes) -> None:
    """Raise :class:`ValueError` when ``path`` contains a NUL byte."""
    if b"\0" in path:
        raise ValueError("socket path must not contain a NUL byte")


def run_long(req: Request, path: bytes, cfg: Config) -> tuple[socket.socket, Route]:
    """:func:`reach_long`'s socket and route."""
    sock, route, _, _ = reach_long(req, path, cfg, Trail())
    return sock, route


def reach_long(req: Request, path: bytes, cfg: Config, trail: Trail) -> Reached:
    """:func:`routes` for a path longer than :data:`DIRECT_MAX`, with the
    socket made to report ``path`` (decoded as the ``socket`` module decodes
    a pathname) as the listener's address or the stream's peer's, rather than
    the spelling the route handed to the kernel (DR-0003)."""
    done = routes(req, path, cfg, trail)
    if isinstance(done.sock, PathSocket):
        done.sock.report_path(os.fsdecode(path), listening=req.op is Op.BIND)
    return done


def routes(req: Request, path: bytes, cfg: Config, trail: Trail) -> Reached:
    """The routes for a path longer than :data:`DIRECT_MAX`, with what they
    read from the environment given as ``cfg`` (the tests pass their own):

    1. the socket's directory is looked up first (``stat``), so a missing
       directory is ``ENOENT`` before any length is judged or anything is
       made. It is then opened for the routes that need its fd (/proc,
       ``connectat``, D in the socket's directory); when it cannot be opened
       (no read permission where it is opened ``O_RDONLY``), those routes
       cannot be used and the others are tried;
    2. the routes through D (:func:`through_d`): a private directory D,
       reached by its fd through a thread's own cwd (Linux, Apple) or
       ``bindat`` / ``connectat`` (FreeBSD), then through /proc (Linux);
    3. where they cannot be used: a bind goes on to :func:`bind_past_d`
       (the form without D where D could be made nowhere, and for
       ``bind_any`` the symlink route), a connect takes its fallbacks
       (:func:`connect_fallbacks`).

    A bind through any of them loses :attr:`Loss.RESPELLED` (``bind_any``'s
    symlink in a shared place :attr:`Loss.RECREATABLE_SPELLING` too); a
    connect loses nothing. The routes are noted in ``trail`` from the
    standard on (it takes no path this long), once the directory has been
    looked up: a failure before that (no such directory, ...) notes nothing
    (SPEC).
    """
    check_no_nul(path)
    directory, name = _path.split(path)
    st = os.stat(directory)
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), os.fsdecode(directory))
    # The realpath R will point to, taken here so that a failure to resolve it
    # is the error it is, before any length is judged (SPEC), rather than the
    # routes through D quietly counting as unusable and ending in ENAMETOOLONG.
    real_dir = os.fsencode(os.path.realpath(directory, strict=True))
    trail.skip(Route.STANDARD, "path too long")
    dirfd: int | None
    try:
        dirfd = open_dir(directory)
    except OSError:
        dirfd = None
    try:
        proc_ok = IS_LINUX and proc_usable(dirfd, cfg.proc_self_fd)
        done = through_d(req, real_dir, name, dirfd, cfg, proc_ok=proc_ok, trail=trail)
        if isinstance(done, tuple):
            losses = (Loss.RESPELLED,) if req.op is Op.BIND else ()
            return Reached(*done, losses, tuple(trail.skipped))
        if req.op is Op.BIND:
            return bind_past_d(req, real_dir, name, dirfd, cfg, proc_ok=proc_ok, why=done, trail=trail)
        sock, route = connect_fallbacks(req, path, directory, name, dirfd, cfg, proc_ok=proc_ok)
        return Reached(sock, route, (), tuple(trail.skipped))
    finally:
        if dirfd is not None:
            os.close(dirfd)


def proc_usable(dirfd: int | None, proc_self_fd: bytes) -> bool:
    """Whether /proc lists this process's fds at ``proc_self_fd``: looked up
    for ``dirfd`` (the socket's directory), or, when that could not be
    opened, for an fd of ``/`` opened for the purpose (Linux)."""
    if dirfd is not None:
        return proc_fd_dir(dirfd, proc_self_fd) is not None
    try:
        probe = open_dir(b"/")
    except OSError:
        return False
    try:
        return proc_fd_dir(probe, proc_self_fd) is not None
    finally:
        os.close(probe)


def bind_past_d(
    req: Request,
    real_dir: bytes,
    name: bytes,
    dirfd: int | None,
    cfg: Config,
    *,
    proc_ok: bool,
    why: Skip,
    trail: Trail,
) -> Reached:
    """A bind the routes through D did not carry (``why``, from
    :func:`through_d`). Where D could be made nowhere
    (:attr:`Skip.NO_OWN_DIR`), the SPEC's form without D is tried first
    (:func:`bind_sockdir`). Past that, and where D could be made but no means
    carried the bind, the SPEC tries the fork routes, which this
    implementation does not have (DR-0001): the default bind answers what a
    plain bind of a path longer than ``sun_path`` answers, ``ENAMETOOLONG``
    (hub QUESTIONS D12-Q3), and ``bind_any`` (``req.at_any_cost``) goes on to
    the symlink route (:func:`bind_symlink_route`), whose ``ENAMETOOLONG``
    ends it where no place carries it either."""
    trail.skip(Route.FORK, NO_FORK)
    if why is Skip.NO_OWN_DIR:
        done = bind_sockdir(req, real_dir, name, dirfd, cfg, proc_ok=proc_ok, trail=trail)
        if done is not None:
            return Reached(*done, (Loss.RESPELLED,), tuple(trail.skipped))
        trail.skip(Route.FORK_SOCKDIR, NO_FORK)
    if req.at_any_cost:
        trail.attempt(Route.SYMLINK)
        done_any = bind_symlink_route(req, real_dir, name, cfg.bind_link_places)
        if done_any is not None:
            sock, losses = done_any
            return Reached(sock, Route.SYMLINK, losses, tuple(trail.skipped))
        trail.trying = None
    raise _enametoolong()


def new_socket(req: Request, *, standard: bool = False) -> socket.socket:
    """A fresh ``AF_UNIX`` / ``SOCK_STREAM`` socket made by the ``socket``
    module (so set up exactly as its own), nonblocking when ``req`` asks: a
    :class:`PathSocket` for the routes of a long path (:func:`run_long` makes
    it report the path given), a plain :class:`socket.socket` when
    ``standard`` (a path the ``socket`` module takes, which gets nothing
    added)."""
    cls = socket.socket if standard else PathSocket
    sock = cls(socket.AF_UNIX, socket.SOCK_STREAM)
    if req.nonblocking:
        sock.setblocking(False)
    return sock


def _finish(req: Request, sock: socket.socket, call: Callable[[socket.socket], None]) -> socket.socket:
    """Run ``call`` on ``sock`` and listen after a bind; close ``sock`` on any
    failure."""
    try:
        call(sock)
        if req.op is Op.BIND:
            _socket_file.bound()
            sock.listen()
    except BaseException:
        sock.close()
        raise
    return sock


def plain_op(req: Request, spelling: str | bytes, *, standard: bool = False) -> socket.socket:
    """Bind (and listen) or connect with ``spelling`` through the ``socket``
    module, as a caller of the standard library would (on a plain
    :class:`socket.socket` when ``standard``, see :func:`new_socket`)."""
    sock = new_socket(req, standard=standard)
    if req.op is Op.BIND:
        return _finish(req, sock, lambda s: s.bind(spelling))
    return _finish(req, sock, lambda s: s.connect(spelling))


def wide_op(req: Request, spelling: bytes) -> socket.socket:
    """Bind (and listen) or connect a fresh socket to ``spelling`` in a
    widened ``sockaddr_un`` (Apple)."""
    addr = sockaddr(spelling, wide=True)
    sock = new_socket(req)
    return _finish(req, sock, lambda s: op(req.op, s, addr))


def at_op(req: Request, dirfd: int, spelling: bytes) -> socket.socket:
    """FreeBSD: ``bindat(2)`` / ``connectat(2)`` of ``spelling`` relative to
    the directory ``dirfd`` (``ENAMETOOLONG`` when it does not fit a
    sockaddr)."""
    addr = sockaddr(spelling)
    sock = new_socket(req)
    return _finish(req, sock, lambda s: op_at(req.op, s, dirfd, addr))


def refuses_wide(e: OSError) -> bool:
    """Whether an error from a widened-address call means the kernel does not
    take the widened form (as opposed to a real outcome such as ``ENOENT``,
    ``EADDRINUSE`` or ``ECONNREFUSED``). XNU reports an address it will not
    take with ``EINVAL``, or ``ENAMETOOLONG`` for one longer than
    ``SOCK_MAXADDRLEN``."""
    return e.errno in (errno.EINVAL, errno.ENAMETOOLONG)


def open_dir(directory: bytes) -> int:
    """Open ``directory`` (the socket's directory, a place for D) to hold it
    by its fd (close-on-exec, as :func:`os.open` makes every fd).

    Linux: ``O_PATH | O_DIRECTORY``, which needs only search permission (the
    fd is usable as the base of the ``*at`` calls and names the directory as
    ``/proc/self/fd/<fd>``).

    Apple: ``O_SEARCH``, which needs only search permission; a kernel that does
    not take ``O_SEARCH`` on a directory answers ``EISDIR`` or ``EINVAL``, and
    then the directory is opened ``O_RDONLY`` (:func:`search_or_read_only`).

    Other systems: ``O_RDONLY | O_DIRECTORY`` (needs read permission).
    """
    if sys.platform == "linux" or sys.platform == "android":
        return os.open(directory, os.O_PATH | os.O_DIRECTORY)
    if sys.platform == "darwin" or sys.platform == "ios":
        return search_or_read_only(directory, lambda: os.open(directory, _APPLE_O_SEARCH))
    return _open_read_only(directory)


_APPLE_O_SEARCH = 0x40000000 | os.O_DIRECTORY
"""``O_SEARCH`` of Apple's ``<sys/fcntl.h>``: ``O_EXEC (0x40000000) |
O_DIRECTORY``.

Design rationale: written out rather than taken from ``os.O_SEARCH``, which
some Python builds for macOS do not have (the python-build-standalone 3.11 of
the CI lacks it; the module defines it only when the build saw the macro).
The value is the system's, not Python's, so it does not depend on the
build."""


def _open_read_only(directory: bytes) -> int:
    return os.open(directory, os.O_RDONLY | os.O_DIRECTORY)


def search_or_read_only(directory: bytes, search: Callable[[], int]) -> int:
    """Open ``directory`` with ``search`` (``O_SEARCH``), unless it fails the
    way a kernel that does not take ``O_SEARCH`` on a directory fails
    (``EISDIR`` or ``EINVAL``); then open it ``O_RDONLY`` instead. Any other
    error (``ENOENT``, ``EACCES``, ...) is the real outcome and is raised as
    is. ``search`` is a parameter so the tests can stand in for such a
    kernel."""
    try:
        return search()
    except OSError as e:
        if e.errno in (errno.EISDIR, errno.EINVAL):
            return _open_read_only(directory)
        raise


def proc_fd_dir(fd: int, proc_self_fd: bytes) -> bytes | None:
    """``<proc_self_fd>/<fd>``, or ``None`` when it cannot be looked up (no
    /proc mounted, as in some chroots and minimal containers)."""
    fd_dir = _join(proc_self_fd, str(fd).encode())
    try:
        os.stat(fd_dir)
    except OSError:
        return None
    return fd_dir


def proc_spelling(dirfd: int, rest: bytes, proc_self_fd: bytes) -> bytes | None:
    """``<proc_self_fd>/<dirfd>/<rest>``, or ``None`` when the spelling cannot
    be used (Linux):

    * ``<proc_self_fd>/<dirfd>`` cannot be looked up (no /proc). Without this
      check a bind / connect through the spelling would fail with ``ENOENT``,
      which reads as "the directory does not exist" although it does;
    * the spelling does not fit ``sun_path``.
    """
    fd_dir = proc_fd_dir(dirfd, proc_self_fd)
    if fd_dir is None:
        return None
    via = _join(fd_dir, rest)
    return via if len(via) <= DIRECT_MAX else None


# ---------- the private directory D ----------

RANDOM_LEN = 10
"""Length of the random part of every name sockat makes: D
(``sockat.XXXXXXXXXX``), R, a connect's temporary symlink."""

OWN_DIR_PREFIX = b"sockat."
"""What D's name starts with in a place for temporary files (and a
connect's temporary symlink's), so one left behind by a process that died is
recognisable as sockat's."""

OWN_DIR_PREFIX_IN_SOCKET_DIR = b".sockat."
"""What D's name starts with in the socket's own directory: the user's
directory, where one left behind is to be recognisable and out of sight."""

ATTEMPTS = 16
"""How many names to try before giving up on a place. Each try is a fresh
random name from 62^10 possibilities, so exhausting this means something
other than bad luck (for example a place that refuses every name)."""

IN_OWN_DIR_LEN = RANDOM_LEN + 1
"""What ``R/`` adds in front of the file name in the D routes'
spelling (``R/<name>``)."""

WITHOUT_OWN_DIR_LEN = len(OWN_DIR_PREFIX_IN_SOCKET_DIR) + RANDOM_LEN + 1
"""What ``.sockat.XXXXXXXXXX/`` adds in front of the file name in the
spelling of a bind's form without D (19 bytes)."""

_ALPHABET = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def random_part() -> bytes:
    """:data:`RANDOM_LEN` random letters and digits from the system's secure
    generator (:mod:`secrets`; when it fails, nothing can be named)."""
    return bytes(secrets.choice(_ALPHABET) for _ in range(RANDOM_LEN))


def random_name(prefix: bytes = OWN_DIR_PREFIX) -> bytes:
    """``prefix`` followed by :func:`random_part`."""
    return prefix + random_part()


def mkdir_at(base_fd: int, name: bytes) -> None:
    """``mkdirat(base_fd, name, 0700)``. A module-level function so the tests
    can replace what is at the name between its creation and the open."""
    os.mkdir(name, 0o700, dir_fd=base_fd)


def _owner_only(st: os.stat_result) -> bool:
    """Whether an opened D is ours: a directory, owned by the effective uid,
    with no permission for group or others."""
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and st.st_mode & 0o077 == 0


@dataclass
class OwnDir:
    """A private directory D made in a place and held by its fd, with a
    symlink in it (R, or a connect's ``s``). :meth:`remove` takes both away
    again."""

    base_fd: int
    """The place, held by its fd."""
    owns_base: bool
    """Whether :attr:`base_fd` was opened for D (and is closed with it), or
    is the caller's (the socket's directory)."""
    name: bytes
    """D's name in the place."""
    fd: int
    """D itself."""
    link: bytes
    """The symlink's name in D."""

    @classmethod
    def make(
        cls,
        places: tuple[OwnDirPlace, ...],
        socket_dirfd: int | None,
        target: bytes,
        *,
        proc_self_fd: bytes,
        link_name: bytes | None = None,
    ) -> OwnDir | None:
        """D in the first of ``places`` where it can be made, holding a
        symlink to ``target`` named ``link_name`` (R, a fresh random name,
        when ``None``). ``None`` when it can be made nowhere.
        ``proc_self_fd`` is where /proc lists the fds (Linux), for setting
        D's mode (:func:`set_own_dir_mode`)."""
        for place in places:
            made = cls._make_in(place, socket_dirfd, target, proc_self_fd, link_name)
            if made is not None:
                return made
        return None

    @classmethod
    def _make_in(
        cls,
        place: OwnDirPlace,
        socket_dirfd: int | None,
        target: bytes,
        proc_self_fd: bytes,
        link_name: bytes | None,
    ) -> OwnDir | None:
        """The SPEC's steps 1 to 3 of the routes through D in one place, with
        step 5 for whatever was made when a later step fails:

        1. open the place (or use the socket's directory's fd) and make D in
           it, mode 0700, under a fresh name (``EEXIST``: another name, up to
           :data:`ATTEMPTS`);
        2. open D (:func:`open_own_dir`), check by ``fstat`` that it is ours
           (:func:`_owner_only`) and set it to 0700 (a umask may have taken
           owner bits off; :func:`set_own_dir_mode`);
        3. create the symlink in D, by its fd.

        ``None`` when any step fails. A D that failed the check may be
        someone else's (put at the name before the open), so it is left
        alone, contents and all; one that passed the check but could not be
        set to 0700 or given the symlink is removed again. One that could not
        be opened is removed when its name passes the same check
        (:func:`_remove_unopened_own_dir`)."""
        if place is SOCKET_DIR:
            if socket_dirfd is None:
                # The socket's directory could not be opened: no place.
                return None
            base_fd, owns_base, prefix = socket_dirfd, False, OWN_DIR_PREFIX_IN_SOCKET_DIR
        else:
            try:
                base_fd = open_dir(place)
            except OSError:
                return None
            owns_base, prefix = True, OWN_DIR_PREFIX
        made: OwnDir | None = None
        try:
            name = _make_dir(base_fd, prefix)
            if name is None:
                return None
            try:
                fd = open_own_dir(base_fd, name)
            except OSError:
                _remove_unopened_own_dir(base_fd, name)
                return None
            verified = False
            try:
                if not _owner_only(os.fstat(fd)):
                    return None
                verified = True
                set_own_dir_mode(fd, base_fd, name, proc_self_fd)
                if link_name is None:
                    link = _make_link(fd, target, random_part)
                else:
                    fixed = link_name
                    link = _make_link(fd, target, lambda: fixed, attempts=1)
                if link is None:
                    return None
                made = cls(base_fd, owns_base, name, fd, link)
                return made
            except OSError:
                return None
            finally:
                if made is None:
                    _remove_own_dir(base_fd, name, fd, verified=verified)
        finally:
            if made is None and owns_base:
                os.close(base_fd)

    def remove(self) -> None:
        """The SPEC's step 5: the symlink (made by this call) through D's fd,
        then D while its name in the place is still the fd's directory, then
        the fds. Failures to remove are ignored."""
        with contextlib.suppress(OSError):
            os.unlink(self.link, dir_fd=self.fd)
        _remove_own_dir(self.base_fd, self.name, self.fd, verified=True)
        if self.owns_base:
            os.close(self.base_fd)


def _make_dir(base_fd: int, prefix: bytes) -> bytes | None:
    """Make D under a fresh name ``prefix + random`` in ``base_fd`` and return
    the name; ``None`` when no name was free after :data:`ATTEMPTS` tries,
    the generator failed or D cannot be made there."""
    for _ in range(ATTEMPTS):
        try:
            name = random_name(prefix)
        except NotImplementedError:
            return None
        try:
            mkdir_at(base_fd, name)
        except FileExistsError:
            continue
        except OSError:
            return None
        return name
    return None


def _make_link(fd: int, target: bytes, draw: Callable[[], bytes], *, attempts: int = ATTEMPTS) -> bytes | None:
    """``symlinkat(target, fd, name)`` under a name from ``draw`` (``EEXIST``:
    another, up to ``attempts``); the name, or ``None`` when it cannot be
    created (the generator failing included)."""
    for _ in range(attempts):
        try:
            name = draw()
        except NotImplementedError:
            return None
        try:
            os.symlink(target, name, dir_fd=fd)
        except FileExistsError:
            continue
        except OSError:
            return None
        return name
    return None


def _remove_own_dir(base_fd: int, name: bytes, fd: int, *, verified: bool) -> None:
    """Remove D (``name`` in ``base_fd``) only when it passed the check and
    the name still refers to the directory ``fd`` holds; close ``fd``."""
    if verified:
        with contextlib.suppress(OSError):
            here = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
            mine = os.fstat(fd)
            if (here.st_dev, here.st_ino) == (mine.st_dev, mine.st_ino):
                os.rmdir(name, dir_fd=base_fd)
    os.close(fd)


def _remove_unopened_own_dir(base_fd: int, name: bytes) -> None:
    """Remove D (``name`` in ``base_fd``) just made but not opened (a umask
    took the owner's search bit off where D is opened ``O_SEARCH``, or the
    read bit where it is opened ``O_RDONLY``) when what is at the name passes
    the check of an opened D (:func:`_owner_only`, looked up without
    following a symlink): otherwise one D would be left in the place per
    call. Something put at the name between the check and the removal is
    removed only when it is an empty directory (``rmdir``). Failures to
    remove are ignored."""
    with contextlib.suppress(OSError):
        if _owner_only(os.stat(name, dir_fd=base_fd, follow_symlinks=False)):
            os.rmdir(name, dir_fd=base_fd)


def open_own_dir(base_fd: int, name: bytes) -> int:
    """Open D (``name`` in ``base_fd``), not following a symlink at the name,
    in a way that needs no read permission on D where the system has one (so
    a umask that took the owner's read bit off D does not stop it):

    * Linux: ``O_PATH | O_DIRECTORY | O_NOFOLLOW``;
    * Apple: ``O_SEARCH | O_DIRECTORY | O_NOFOLLOW``, or ``O_RDONLY`` when
      the kernel does not take ``O_SEARCH`` on a directory
      (:func:`search_or_read_only`'s rule);
    * every other system: ``O_RDONLY | O_DIRECTORY | O_NOFOLLOW``.
    """
    if sys.platform == "linux" or sys.platform == "android":
        return os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base_fd)
    read_only = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if sys.platform == "darwin" or sys.platform == "ios":
        try:
            return os.open(name, _APPLE_O_SEARCH | os.O_NOFOLLOW, dir_fd=base_fd)
        except OSError as e:
            if e.errno not in (errno.EISDIR, errno.EINVAL):
                raise
    return os.open(name, read_only, dir_fd=base_fd)


def set_own_dir_mode(fd: int, base_fd: int, name: bytes, proc_self_fd: bytes) -> None:
    """Set D (held by ``fd``, named ``name`` in ``base_fd``) to exactly 0700,
    acting on what the fd holds, not on whatever is at the name now.

    Linux (an ``O_PATH`` fd, which answers ``fchmod`` with ``EBADF``): through
    ``<proc_self_fd>/<fd>`` where /proc can be used; otherwise D is opened
    again by its name ``O_RDONLY | O_DIRECTORY | O_NOFOLLOW``, checked to be
    the same ``(st_dev, st_ino)`` as ``fd``, and set by ``fchmod`` on that.
    Every other system: ``fchmod(fd)``.

    Raises :class:`OSError` when it cannot be done (the reopening refused:
    a umask took the owner's read bit off, as for a user other than root; the
    name no longer D)."""
    if not (sys.platform == "linux" or sys.platform == "android"):
        os.fchmod(fd, 0o700)
        return
    fd_dir = proc_fd_dir(fd, proc_self_fd)
    if fd_dir is not None:
        os.chmod(fd_dir, 0o700)
        return
    again = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base_fd)
    try:
        a, b = os.fstat(again), os.fstat(fd)
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise OSError(errno.ESTALE, "the private directory was replaced before its mode was set")
        os.fchmod(again, 0o700)
    finally:
        os.close(again)


# ---------- the routes through D ----------


class Skip(Enum):
    """Why a route did not run."""

    UNUSABLE = "unusable"
    """The route could not be used (too long, nothing to hold, the means
    refused)."""
    NO_OWN_DIR = "no-own-dir"
    """The routes through D had a means to try, but D could be made in none of
    the places (a bind then tries the form without D)."""
    WIDE_REFUSED = "wide-refused"
    """The spelling needed the widened form and the kernel refused it
    (Apple)."""


class Means(Enum):
    """A way of reaching ``R/<name>`` from D's fd."""

    THREAD_CWD = "thread-cwd"
    AT = "at"
    PROC = "proc"


THROUGH_D = {Means.THREAD_CWD: Route.THREAD_CWD, Means.AT: Route.AT, Means.PROC: Route.PROC}
"""The route each means makes with D."""

SOCKDIR = {Means.THREAD_CWD: Route.THREAD_CWD_SOCKDIR, Means.AT: Route.AT_SOCKDIR, Means.PROC: Route.PROC_SOCKDIR}
"""The route each means makes in the form without D."""

TOO_LONG = "spelling too long"
NO_PLACE_FOR_D = "no place for D"
NO_FORK = "no fork route in sockat-py"


def means_through_d(name: bytes, dirfd: int | None, cfg: Config, *, proc_ok: bool) -> list[tuple[Means, str | None]]:
    """The means of the routes through D for ``name`` (:func:`judge_means`
    of ``R/<name>``), judged as far as they can be before D is made (SPEC:
    nothing is made for means that cannot be used)."""
    # D's fd is not known before D is opened; the socket directory's (or a
    # two-digit one when that is not open) stands in for /proc, and the exact
    # length is checked again with D's own fd.
    return judge_means(b"R" * RANDOM_LEN + b"/" + name, dirfd if dirfd is not None else 10, cfg, proc_ok=proc_ok)


def judge_means(spelling: bytes, fd: int, cfg: Config, *, proc_ok: bool) -> list[tuple[Means, str | None]]:
    """Every means this system has, in order, each with ``None`` when it can
    be tried for ``spelling`` handed over relative to a directory held by the
    fd numbered ``fd``, or else why not (a short phrase, for diagnosis):

    * FreeBSD: ``bindat`` / ``connectat``, when ``spelling`` fits a sockaddr;
    * Linux, Apple: a thread's own cwd, when ``spelling`` fits a sockaddr,
      it is not turned off and the system may give one
      (:func:`._sys.thread_cwd_possible`);
    * Linux: /proc, when it can be used and ``/proc/self/fd/<fd>/<spelling>``
      fits a sockaddr.
    """
    fits = len(spelling) <= DIRECT_MAX
    if IS_FREEBSD:
        return [(Means.AT, None if fits else TOO_LONG)]
    judged: list[tuple[Means, str | None]] = []
    if HAS_THREAD_CWD:
        why: str | None = None
        if not fits:
            why = TOO_LONG
        elif not cfg.thread_cwd:
            why = "thread cwd turned off"
        elif not thread_cwd_possible():
            why = "seccomp filter" if IS_LINUX else "no pthread_fchdir_np"
        judged.append((Means.THREAD_CWD, why))
    if IS_LINUX:
        if not proc_ok:
            judged.append((Means.PROC, "/proc unusable"))
        elif len(_join(_join(cfg.proc_self_fd, str(fd).encode()), spelling)) > DIRECT_MAX:
            judged.append((Means.PROC, TOO_LONG))
        else:
            judged.append((Means.PROC, None))
    return judged


def means_for(spelling: bytes, fd: int, cfg: Config, *, proc_ok: bool) -> list[Means]:
    """The means of :func:`judge_means` that can be tried, in order."""
    return [m for m, why in judge_means(spelling, fd, cfg, proc_ok=proc_ok) if why is None]


def _note_all(judged: list[tuple[Means, str | None]], routes_of: dict[Means, Route], why: str, trail: Trail) -> None:
    """Note every judged means as skipped: an unusable one for its own
    reason, a usable one for ``why`` (what stopped it before it was
    tried)."""
    for m, own in judged:
        trail.skip(routes_of[m], own if own is not None else why)


def _by_means(
    req: Request,
    judged: list[tuple[Means, str | None]],
    routes_of: dict[Means, Route],
    base_fd: int,
    spelling: bytes,
    cfg: Config,
    trail: Trail,
) -> tuple[socket.socket, Route] | None:
    """Bind / connect ``spelling`` relative to the directory ``base_fd`` by
    the first of the ``judged`` means that carries it, in order (each makes
    the route ``routes_of`` names); every means before it is noted in
    ``trail`` with why it was skipped. ``None`` when none carries it. A
    failure of the bind / connect itself is the outcome and is raised."""
    for m, why in judged:
        route = routes_of[m]
        if why is not None:
            trail.skip(route, why)
        elif m is Means.AT:
            trail.attempt(route)
            return at_op(req, base_fd, spelling), route
        elif m is Means.THREAD_CWD:
            trail.attempt(route)
            done = thread_cwd_op(req, base_fd, spelling)
            if isinstance(done, socket.socket):
                return done, route
            trail.skip(route, "thread cwd refused")
        else:
            via = proc_spelling(base_fd, spelling, cfg.proc_self_fd)
            if via is None:
                trail.skip(route, TOO_LONG if proc_fd_dir(base_fd, cfg.proc_self_fd) is not None else "/proc unusable")
                continue
            trail.attempt(route)
            return plain_op(req, via), route
    return None


def through_d(
    req: Request, real_dir: bytes, name: bytes, dirfd: int | None, cfg: Config, *, proc_ok: bool, trail: Trail
) -> tuple[socket.socket, Route] | Skip:
    """The routes through D (SPEC): D in the first of
    :attr:`Config.own_dir_places` where it can be made (:class:`OwnDir`),
    holding R, a symlink to ``real_dir`` (the realpath of the socket's
    directory, taken by :func:`routes`), and the
    bind / connect of ``R/<name>`` from D's fd by the first means that can be
    used (:func:`means_through_d`). D and R are removed again whatever the
    outcome, so the spelling the kernel keeps (``R/<name>``) names nothing
    afterwards, from any cwd. Each route that did not carry it is noted in
    ``trail``.

    Nothing is left behind when it does not carry the call:
    :attr:`Skip.NO_OWN_DIR` when D can be made nowhere,
    :attr:`Skip.UNUSABLE` when no means can be used (nothing is made) or
    every means turned out unusable once tried. A failure of the bind /
    connect itself is the outcome and is raised."""
    judged = means_through_d(name, dirfd, cfg, proc_ok=proc_ok)
    if all(why is not None for _, why in judged):
        _note_all(judged, THROUGH_D, "", trail)
        return Skip.UNUSABLE
    own = OwnDir.make(cfg.own_dir_places, dirfd, real_dir, proc_self_fd=cfg.proc_self_fd)
    if own is None:
        _note_all(judged, THROUGH_D, NO_PLACE_FOR_D, trail)
        return Skip.NO_OWN_DIR
    try:
        done = _by_means(req, judged, THROUGH_D, own.fd, own.link + b"/" + name, cfg, trail)
        return Skip.UNUSABLE if done is None else done
    finally:
        own.remove()


def bind_sockdir(
    req: Request, real_dir: bytes, name: bytes, dirfd: int | None, cfg: Config, *, proc_ok: bool, trail: Trail
) -> tuple[socket.socket, Route] | None:
    """The SPEC's form without D, for a bind where D could be made nowhere:
    R (``.sockat.`` and :data:`RANDOM_LEN` letters or digits, a symlink to
    ``real_dir``, the realpath of the socket's directory) made in the
    socket's directory itself through its fd ``dirfd`` (``EEXIST``: another
    name, up to :data:`ATTEMPTS`), and the bind of ``R/<name>`` from that fd
    by the means of the routes through D in their order: a thread's own cwd
    (Linux, Apple) or ``bindat`` (FreeBSD), then
    ``/proc/self/fd/<dirfd>/R/<name>`` (Linux). R is removed again whatever
    the outcome, so the spelling the kernel keeps names nothing afterwards.
    Each route that did not carry it is noted in ``trail``.

    ``None`` when the form cannot be used: the socket's directory could not
    be opened, no means can be used for the spelling (19 bytes and the file
    name: up to 88 bytes of name on Linux, 84 on Apple and the BSDs, fewer
    through /proc), both checked before anything is made; R cannot be
    created; or every means turned out unusable once tried. A failure of the
    bind itself is the outcome and is raised."""
    sample = OWN_DIR_PREFIX_IN_SOCKET_DIR + b"R" * RANDOM_LEN + b"/" + name
    if dirfd is None:
        judged = judge_means(sample, 10, cfg, proc_ok=proc_ok)
        _note_all([(m, None) for m, _ in judged], SOCKDIR, "socket directory not opened", trail)
        return None
    judged = judge_means(sample, dirfd, cfg, proc_ok=proc_ok)
    if all(why is not None for _, why in judged):
        _note_all(judged, SOCKDIR, "", trail)
        return None
    link = _make_link(dirfd, real_dir, lambda: random_name(OWN_DIR_PREFIX_IN_SOCKET_DIR))
    if link is None:
        _note_all(judged, SOCKDIR, "R not created", trail)
        return None
    try:
        return _by_means(req, judged, SOCKDIR, dirfd, link + b"/" + name, cfg, trail)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(link, dir_fd=dirfd)


def thread_cwd_op(req: Request, dirfd: int, spelling: bytes) -> socket.socket | Skip:
    """Bind (and listen) or connect a fresh socket to ``spelling`` from a
    throwaway :class:`threading.Thread` whose own cwd is the directory
    ``dirfd`` (:func:`._sys.enter_dir_in_this_thread`: ``unshare(CLONE_FS)``
    then ``fchdir(2)`` on Linux, ``pthread_fchdir_np`` on Apple). The process
    cwd, and so every other thread's, never changes, nor does the calling
    thread's; the thread ends with the call.

    :attr:`Skip.UNUSABLE` when the thread cannot be started, or the system
    refuses a cwd of the thread's own (a seccomp filter on, as under docker's
    default, or ``unshare`` refused; no ``pthread_fchdir_np``, or it failed).
    Any other failure (the ``fchdir`` after ``unshare`` included) is the
    outcome.
    """
    sock = new_socket(req)
    outcome: list[BaseException | Skip | None] = []

    def in_thread() -> None:
        try:
            if not enter_dir_in_this_thread(dirfd):
                outcome.append(Skip.UNUSABLE)
                return
            if req.op is Op.BIND:
                sock.bind(spelling)
            else:
                sock.connect(spelling)
            outcome.append(None)
        except BaseException as e:
            outcome.append(e)

    thread = threading.Thread(target=in_thread, name="sockat-thread-cwd")
    try:
        thread.start()
    except RuntimeError:
        sock.close()
        return Skip.UNUSABLE
    # The thread uses `dirfd` (D, or the socket's directory for the form
    # without D, which the caller closes as soon as this returns) and `sock`:
    # whatever interrupts the wait (an exception such as KeyboardInterrupt out
    # of join), the thread is waited for to the end first, so it never enters
    # a closed (or reused) fd. The interruption is raised then, with the
    # socket closed.
    interrupted: BaseException | None = None
    while True:
        try:
            thread.join()
            break
        except BaseException as e:
            if interrupted is None:
                interrupted = e
    if interrupted is not None:
        sock.close()
        raise interrupted
    result = outcome[0] if outcome else RuntimeError("the thread-cwd thread ended without an outcome")
    if result is Skip.UNUSABLE:
        sock.close()
        return Skip.UNUSABLE
    if isinstance(result, BaseException):
        sock.close()
        raise result
    return _finish(req, sock, lambda s: None)


# ---------- a connect's fallbacks ----------

SPELLING_MAX = WIDE_PATH_MAX if IS_APPLE else DIRECT_MAX
"""The longest spelling a connect's sockaddr carries on this system: the
widened form's 253 bytes on Apple, :data:`DIRECT_MAX` elsewhere."""


def connect_fallbacks(
    req: Request,
    path: bytes,
    directory: bytes,
    name: bytes,
    dirfd: int | None,
    cfg: Config,
    *,
    proc_ok: bool,
) -> tuple[socket.socket, Route]:
    """A connect the routes through D did not carry, by the SPEC's fallbacks
    for this system, in order:

    ================================ =========================================
    system                           order
    ================================ =========================================
    FreeBSD                          connectat of the file name on ``dirfd``
    Linux with /proc (``proc_ok``)   /proc → proc-held → symlink
    Apple                            relative → widened sockaddr → symlink
    every other system               relative → symlink
    ================================ =========================================

    The spellings carry no random part (a connected socket is not bound, so
    its spelling is left nowhere). A failure of a connect is the outcome;
    only a fallback that cannot be used moves on. Past the last one:
    ``ENAMETOOLONG`` (the SPEC's answer for ``connect`` where only the fork
    route would reach).
    """
    if IS_FREEBSD:
        if dirfd is None:
            raise _enametoolong()
        return at_op(req, dirfd, name), Route.AT_DIRECT
    wide = IS_APPLE and cfg.wide
    if proc_ok:
        via = proc_spelling(dirfd, name, cfg.proc_self_fd) if dirfd is not None else None
        if via is not None:
            return plain_op(req, via), Route.PROC_DIRECT
        held = proc_held_route(req, path, dirfd, cfg)
        if isinstance(held, tuple):
            return held
    else:
        rel = Relative.new(path, directory)
        if rel is not None and len(rel.path) <= DIRECT_MAX:
            return rel.run(lambda p: plain_op(req, p)), Route.RELATIVE
        if wide:
            try:
                if len(path) <= WIDE_PATH_MAX:
                    return wide_op(req, path), Route.WIDENED
                if rel is not None and len(rel.path) <= WIDE_PATH_MAX:
                    return rel.run(lambda p: wide_op(req, p)), Route.WIDENED
            except OSError as e:
                if not refuses_wide(e):
                    raise
                wide = False
    for base in cfg.link_places:
        outcome = symlink_route(req, base, path, WIDE_PATH_MAX if wide else DIRECT_MAX)
        if isinstance(outcome, tuple):
            return outcome
        if outcome is Skip.WIDE_REFUSED:
            wide = False
    raise _enametoolong()


def _absolute(path: bytes) -> bytes | None:
    """``path`` made absolute against the cwd (as the kernel resolves it),
    or ``None`` when the cwd cannot be read."""
    if path.startswith(b"/"):
        return path
    try:
        return _join(os.getcwdb(), path)
    except OSError:
        return None


def proc_held_route(req: Request, path: bytes, dirfd: int | None, cfg: Config) -> tuple[socket.socket, Route] | Skip:
    """Connect to ``path`` through D (made as the routes through D make it, in
    the first of :attr:`Config.own_dir_places` where it can be, steps 1, 2
    and 5) holding ``s``, a symlink to the socket file (``path`` made
    absolute), by ``/proc/self/fd/<fd of D>/s`` (Linux with /proc).

    :attr:`Skip.UNUSABLE` when /proc cannot be used or D and ``s`` can be
    made nowhere. The outcome of the connect is returned as it is. D and
    ``s`` are removed again whatever the outcome."""
    if not IS_LINUX:
        return Skip.UNUSABLE
    target = _absolute(path)
    if target is None:
        return Skip.UNUSABLE
    own = OwnDir.make(cfg.own_dir_places, dirfd, target, proc_self_fd=cfg.proc_self_fd, link_name=b"s")
    if own is None:
        return Skip.UNUSABLE
    try:
        via = proc_spelling(own.fd, own.link, cfg.proc_self_fd)
        if via is None:
            return Skip.UNUSABLE
        return plain_op(req, via), Route.PROC_HELD
    finally:
        own.remove()


def _dir_id(directory: bytes) -> tuple[int, int] | None:
    try:
        st = os.stat(directory)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _cwd_spellings(cwd: bytes, cwd_id: tuple[int, int]) -> list[bytes]:
    """The spellings of the cwd that the path may start with: what
    ``getcwd(3)`` reports (a real path), and ``$PWD`` (the path the shell used
    to get there, possibly through symlinks) but only when it names the cwd.

    ``$PWD`` is just an environment variable: it is stale after a ``chdir(2)``
    that did not update it, and anyone can set it. It is used only when
    ``stat($PWD)`` and ``stat(".")`` agree on ``(st_dev, st_ino)``.
    """
    spellings = [cwd]
    pwd = os.environb.get(b"PWD")
    if pwd and pwd.startswith(b"/") and pwd != cwd and _dir_id(pwd) == cwd_id:
        spellings.append(pwd)
    return spellings


class CwdChanged(OSError):
    """Raised by :meth:`Relative.run` when the cwd changed during the call.
    Not exported: callers see an :class:`OSError` without an errno."""


@dataclass(frozen=True)
class Relative:
    """A relative spelling of an absolute socket path, checked to name the
    same directory as the absolute one."""

    path: bytes
    cwd: tuple[int, int] = field(repr=False)
    """``(st_dev, st_ino)`` of the cwd when the spelling was made."""

    @classmethod
    def new(cls, path: bytes, directory: bytes) -> Relative | None:
        """Spell ``path`` (whose directory part is ``directory``) relative to
        the cwd.

        Two ways, the first one first:

        1. **Below the cwd.** When ``path`` starts, component by component,
           with a spelling of the cwd (``getcwd(3)``, or a ``$PWD`` that names
           the cwd), cut that prefix off. The rest only descends from the cwd
           along ``path`` itself, so it resolves the same way ``path`` does.
        2. **Through real paths.** Only when 1 gives nothing of at most
           :data:`DIRECT_MAX` bytes: resolve ``directory`` with ``realpath(3)``
           and climb out of the cwd as ``getcwd(3)`` reports it (both real, so
           every ``..`` climbs out of a real directory, as the kernel does).

        The shorter result is kept. ``None`` when ``path`` is not absolute,
        the cwd cannot be read, neither way gives a spelling a sockaddr of
        this system carries (:data:`SPELLING_MAX`), or the relative directory
        turns out not to be the absolute one.
        """
        if not path.startswith(b"/"):
            return None
        _, name = _path.split(path)
        cwd_before = _dir_id(b".")
        if cwd_before is None:
            return None
        try:
            cwd = os.getcwdb()
        except OSError:
            return None

        best: bytes | None = None
        for prefix in _cwd_spellings(cwd, cwd_before):
            rel = _path.below(path, prefix)
            if rel is not None and (best is None or len(rel) < len(best)):
                best = rel
        if best is None or len(best) > DIRECT_MAX:
            try:
                real_dir = os.fsencode(os.path.realpath(directory, strict=True))
            except OSError:
                real_dir = None
            if real_dir is not None:
                rel = _path.relative(_join(real_dir, name), cwd)
                if rel is not None and (best is None or len(rel) < len(best)):
                    best = rel
        if best is None or len(best) > SPELLING_MAX:
            return None
        rel_dir, _ = _path.split(best)
        rel_id = _dir_id(rel_dir)
        if rel_id is None or rel_id != _dir_id(directory):
            return None
        if _dir_id(b".") != cwd_before:
            return None
        return cls(path=best, cwd=cwd_before)

    def run(self, f: Callable[[bytes], socket.socket]) -> socket.socket:
        """Run ``f`` with the relative spelling, then confirm the cwd is still
        the one the spelling was made against.

        If another thread changed the cwd in between, the call went to some
        other directory: the result is discarded (a socket is closed) and
        :class:`CwdChanged` is raised. The check cannot see a change that is
        undone before it runs; the guarantee is for processes that do not
        change the cwd while connecting.
        """
        result: socket.socket | None = None
        failure: OSError | None = None
        try:
            result = f(self.path)
        except OSError as e:
            failure = e
        if _dir_id(b".") != self.cwd:
            if result is not None:
                result.close()
            raise CwdChanged(
                "the current directory changed while connecting through a path relative to it; the result was discarded"
            ) from failure
        if failure is not None:
            raise failure
        assert result is not None
        return result


# ---------- the symlink route ----------


def symlink_route(req: Request, base: bytes, path: bytes, limit: int) -> tuple[socket.socket, Route] | Skip:
    """A connect's symlink route: connect to ``path`` through a symlink
    ``<base>/sockat.XXXXXXXXXX`` to the socket file (``path`` made absolute
    against the cwd, so the kernel resolves the link as it resolves
    ``path``). A connect follows a symlink in the last component, so the
    spelling's length depends on neither the directory nor the file name.

    Used when the spelling is at most ``limit`` bytes (:data:`DIRECT_MAX` for
    the plain sockaddr, on Apple the widened form's 253) and ``base`` is a
    place where nobody else can replace the symlink (:func:`safe_base`). Both
    are checked before anything is created. Nothing is left under ``base``:
    the symlink is removed when this returns, whatever the outcome.
    """
    if len(_join(base, OWN_DIR_PREFIX + b"X" * RANDOM_LEN)) > limit or not safe_base(base):
        return Skip.UNUSABLE
    target = _absolute(path)
    if target is None:
        return Skip.UNUSABLE
    try:
        link = TempLink.create(base, target)
    except (OSError, NotImplementedError):
        return Skip.UNUSABLE
    with link:
        if IS_APPLE and len(link.path) > DIRECT_MAX:
            try:
                return wide_op(req, link.path), Route.SYMLINK
            except OSError as e:
                if refuses_wide(e):
                    return Skip.WIDE_REFUSED
                raise
        return plain_op(req, link.path), Route.SYMLINK


def bind_symlink_route(
    req: Request, real_dir: bytes, name: bytes, places: tuple[bytes, ...]
) -> tuple[socket.socket, tuple[Loss, ...]] | None:
    """``bind_any``'s symlink route, its last: bind ``<place>/sockat.XXXXXXXXXX/<name>``
    through a symlink to ``real_dir`` (the realpath of the socket's
    directory) made in one of ``places`` and removed again whatever the
    outcome.

    Every place only we can write to (:attr:`PlaceKind.OWN`) is tried
    before any shared one (:attr:`PlaceKind.SHARED`), each kind in the order
    of ``places``; a place of neither kind is not used. Before anything is
    made in a place, the spelling is checked to fit the plain sockaddr (a
    bind never takes the widened one); a place where the symlink cannot be
    created is passed over. A failure of the bind itself is the outcome and
    is raised.

    Returns the listener and what was lost: :attr:`Loss.RESPELLED` through a
    place of our own (nobody else can make the spelling name anything once
    the symlink is gone), and :attr:`Loss.RECREATABLE_SPELLING` too through
    a shared one. ``None`` when no place carries the bind."""
    own_only: tuple[Loss, ...] = (Loss.RESPELLED,)
    shared: tuple[Loss, ...] = (Loss.RESPELLED, Loss.RECREATABLE_SPELLING)
    kinds = [(place, place_kind(place)) for place in places]
    ordered = [(place, own_only) for place, kind in kinds if kind is PlaceKind.OWN]
    ordered += [(place, shared) for place, kind in kinds if kind is PlaceKind.SHARED]
    for place, losses in ordered:
        if len(_join(_join(place, OWN_DIR_PREFIX + b"X" * RANDOM_LEN), name)) > DIRECT_MAX:
            continue
        try:
            link = TempLink.create(place, real_dir)
        except (OSError, NotImplementedError):
            continue
        with link:
            return plain_op(req, _join(link.path, name)), losses
    return None


class PlaceKind(Enum):
    """What a place for a temporary symlink is, for who can replace the
    entries made there (:func:`place_kind_of`)."""

    OWN = "own"
    """Owned by us with no write permission for group and others
    (``mode & 0o022 == 0``, as a private ``TMPDIR`` or ``XDG_RUNTIME_DIR``
    is): nobody else can create, remove or rename entries there at all."""
    SHARED = "shared"
    """Sticky (``S_ISVTX``, as ``/tmp`` is) and owned by root or us: an entry
    can then be removed or renamed only by its owner, the directory's owner
    and root, but anyone may create one at a name not taken."""


def place_kind(base: bytes) -> PlaceKind | None:
    """What ``base`` is as a place for a temporary symlink, followed through
    symlinks (macOS's ``/tmp`` is one to ``/private/tmp``); ``None`` when it
    is of neither kind, cannot be looked up or is not a directory."""
    try:
        st = os.stat(base)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    return place_kind_of(st.st_mode, st.st_uid, os.geteuid())


def place_kind_of(mode: int, owner: int, us: int) -> PlaceKind | None:
    """What a directory of ``mode`` owned by ``owner`` is to the user ``us``
    (:class:`PlaceKind`): :attr:`PlaceKind.OWN` where it is both."""
    if owner == us and mode & 0o022 == 0:
        return PlaceKind.OWN
    if mode & stat.S_ISVTX and owner in (us, 0):
        return PlaceKind.SHARED
    return None


def safe_base(base: bytes) -> bool:
    """Whether ``base`` is a place where no other user (root aside) can
    remove or rename an entry (:func:`place_kind`), so the connect's symlink
    cannot be replaced between its creation and the connect through it."""
    return place_kind(base) is not None


def only_we_can_replace(mode: int, owner: int, us: int) -> bool:
    """Whether, in a directory of ``mode`` owned by ``owner``, only the user
    ``us`` and root can remove or rename the entries ``us`` creates (either
    :class:`PlaceKind`)."""
    return place_kind_of(mode, owner, us) is not None


class TempLink:
    """A symlink under a base directory with a random name, removed when the
    ``with`` block ends."""

    def __init__(self, path: bytes) -> None:
        self.path = path

    @classmethod
    def create(
        cls,
        base: bytes,
        target: bytes,
        next_name: Callable[[], bytes] = random_name,
    ) -> TempLink:
        """Create a symlink under ``base`` pointing to ``target``, named by
        ``next_name`` (called once per try; its error ends the attempt).

        ``symlink(2)`` fails with ``EEXIST`` instead of replacing an existing
        entry, which is the ``O_EXCL`` of symlinks: the link that is created is
        always one nobody else had, and a name already taken (by anyone) is
        skipped for the next one, up to :data:`ATTEMPTS` tries. Any other
        error ends the attempt.
        """
        for _ in range(ATTEMPTS):
            link = _join(base, next_name())
            try:
                os.symlink(target, link)
            except FileExistsError:
                continue
            return cls(link)
        raise FileExistsError(errno.EEXIST, "could not find a free name for a temporary symlink")

    def __enter__(self) -> TempLink:
        return self

    def __exit__(self, *exc: object) -> None:
        # Best effort: the socket is already connected, and failing it now
        # would not bring the symlink back either.
        with contextlib.suppress(OSError):
            os.unlink(self.path)
