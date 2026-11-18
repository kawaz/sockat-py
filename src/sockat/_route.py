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
from enum import Enum
from typing import Literal, TypeAlias

from . import _path
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


BIND = Request(Op.BIND)
CONNECT = Request(Op.CONNECT)
CONNECT_NONBLOCKING = Request(Op.CONNECT, nonblocking=True)


class Route(Enum):
    """Which way a bind / connect went. The public API does not show it."""

    DIRECT = "direct"
    """The path itself, through the ``socket`` module."""
    THREAD_CWD = "thread-cwd"
    """The central route: ``R/<name>`` from a throwaway thread whose own cwd
    is the private directory D (Linux, Apple)."""
    AT = "at"
    """The central route: ``bindat(2)`` / ``connectat(2)`` of ``R/<name>`` on
    the fd of D (FreeBSD)."""
    PROC_OWN_DIR = "proc-own-dir"
    """The central route through /proc: ``/proc/self/fd/<fd of D>/R/<name>``,
    through the ``socket`` module (Linux)."""
    PROC_FD = "proc-fd"
    """A connect's fallback: ``/proc/self/fd/<fd of the socket's
    directory>/<name>``, through the ``socket`` module (Linux)."""
    FD_HELD = "fd-held"
    """A connect's fallback: ``/proc/self/fd/<fd of D>/s``, ``s`` a symlink
    to the socket file (Linux)."""
    RELATIVE = "relative"
    """A connect's fallback: the path spelled relative to the cwd, through
    the ``socket`` module."""
    WIDE = "wide"
    """A connect's fallback: the path itself in a widened ``sockaddr_un``
    (Apple)."""
    WIDE_RELATIVE = "wide-relative"
    """A connect's fallback: the relative spelling in a widened
    ``sockaddr_un`` (Apple)."""
    SYMLINK_TO_FILE = "symlink-to-file"
    """A connect's fallback: a short temporary symlink to the socket file."""
    SYMLINK_TO_FILE_WIDE = "symlink-to-file-wide"
    """:attr:`SYMLINK_TO_FILE` in a widened ``sockaddr_un`` (Apple)."""
    AT_DIR = "at-dir"
    """A connect's fallback: ``connectat(2)`` of the file name on the fd of
    the socket's directory (FreeBSD)."""


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
    wide: bool = IS_APPLE
    """Whether a connect tries the widened ``sockaddr_un`` at all (Apple).
    The tests turn it off to stand in for a kernel that refuses the widened
    form."""
    proc_self_fd: bytes = b"/proc/self/fd"
    """Where the kernel lists the fds of the process (Linux). The tests point
    it elsewhere to stand in for a system without /proc."""
    thread_cwd: bool = HAS_THREAD_CWD
    """Whether the central route tries a thread's own cwd at all (Linux,
    Apple). The tests turn it off to stand in for a system where it cannot be
    used."""

    @classmethod
    def from_env(cls) -> Config:
        return cls.from_tmpdir(os.environb.get(b"TMPDIR"))

    @classmethod
    def from_tmpdir(cls, tmpdir: bytes | None) -> Config:
        """The configuration for a ``TMPDIR`` of ``tmpdir``.

        Places for D: ``${TMPDIR:-/tmp}`` (unset or empty: ``/tmp``; a
        relative one is opened against the cwd once and held by its fd),
        ``/tmp`` unless the first is ``/tmp`` itself, then the socket's
        directory.

        Places for a connect's symlink: ``TMPDIR`` when it is absolute and
        not ``/tmp`` itself (a relative one would make the link's place
        depend on the cwd), then ``/tmp``."""
        first = tmpdir or b"/tmp"
        own: list[OwnDirPlace] = [first]
        if not _same_spelling(first, b"/tmp"):
            own.append(b"/tmp")
        own.append(SOCKET_DIR)
        links: list[bytes] = []
        if tmpdir and tmpdir.startswith(b"/") and not _same_spelling(tmpdir, b"/tmp"):
            links.append(tmpdir)
        links.append(b"/tmp")
        return cls(own_dir_places=tuple(own), link_places=tuple(links))


def run(req: Request, path: StrOrBytesPath) -> tuple[socket.socket, Route]:
    """Bind (and listen) or connect a stream socket to ``path``, trying the
    routes in order.

    A path the ``socket`` module takes (at most :data:`DIRECT_MAX` bytes) goes
    to it as is, before anything else is looked at (the environment included).

    A NUL byte makes the path an argument error whatever its length (the
    ``socket`` module would silently cut the path there), except a leading NUL
    on Linux / Android: that is an address in the abstract namespace, not a
    pathname, and goes to the ``socket`` module as is whatever its length.
    """
    spelled = os.fspath(path)
    raw = os.fsencode(spelled)
    if IS_LINUX and raw.startswith(b"\0"):
        return plain_op(req, spelled, standard=True), Route.DIRECT
    check_no_nul(raw)
    if len(raw) <= DIRECT_MAX:
        return plain_op(req, spelled, standard=True), Route.DIRECT
    return run_long(req, raw, Config.from_env())


def check_no_nul(path: bytes) -> None:
    """Raise :class:`ValueError` when ``path`` contains a NUL byte."""
    if b"\0" in path:
        raise ValueError("socket path must not contain a NUL byte")


def run_long(req: Request, path: bytes, cfg: Config) -> tuple[socket.socket, Route]:
    """:func:`routes` for a path longer than :data:`DIRECT_MAX`, with the
    socket made to report ``path`` (decoded as the ``socket`` module decodes
    a pathname) as the listener's address or the stream's peer's, rather than
    the spelling the route handed to the kernel (DR-0003)."""
    sock, route = routes(req, path, cfg)
    if isinstance(sock, PathSocket):
        sock.report_path(os.fsdecode(path), listening=req.op is Op.BIND)
    return sock, route


def routes(req: Request, path: bytes, cfg: Config) -> tuple[socket.socket, Route]:
    """The routes for a path longer than :data:`DIRECT_MAX`, with what they
    read from the environment given as ``cfg`` (the tests pass their own):

    1. the socket's directory is opened first, so a missing or unopenable
       directory is the real error it is, before any length is judged or
       anything is made;
    2. the central route (:func:`central_route`): a private directory D,
       reached by its fd through a thread's own cwd (Linux, Apple) or
       ``bindat`` / ``connectat`` (FreeBSD), then through /proc (Linux);
    3. where it cannot be used: a bind ends at :func:`bind_without_central`,
       a connect takes its fallbacks (:func:`connect_fallbacks`).
    """
    check_no_nul(path)
    directory, name = _path.split(path)
    dirfd = open_dir(directory)
    try:
        proc_ok = IS_LINUX and proc_fd_dir(dirfd, cfg.proc_self_fd) is not None
        done = central_route(req, directory, name, dirfd, cfg, proc_ok=proc_ok)
        if done is not None:
            return done
        if req.op is Op.BIND:
            return bind_without_central(req, path, directory, name, cfg)
        return connect_fallbacks(req, path, directory, name, dirfd, cfg, proc_ok=proc_ok)
    finally:
        os.close(dirfd)


def bind_without_central(
    req: Request, path: bytes, directory: bytes, name: bytes, cfg: Config
) -> tuple[socket.socket, Route]:
    """A bind the central route did not carry. The SPEC tries the fork route
    here (with D, or without it when D could be made nowhere), which this
    implementation does not have (DR-0001): what a plain bind of a path longer
    than ``sun_path`` answers, ``ENAMETOOLONG``.

    Kept as one function so that what Python does in the fork route's place
    can be decided in one spot (hub QUESTIONS D12-Q3)."""
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
"""What ``R/`` adds in front of the file name in the central route's
spelling (``R/<name>``)."""

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
        socket_dirfd: int,
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
        socket_dirfd: int,
        target: bytes,
        proc_self_fd: bytes,
        link_name: bytes | None,
    ) -> OwnDir | None:
        """The SPEC's steps 1 to 3 of the central route in one place, with
        step 5 for whatever was made when a later step fails:

        1. open the place (or use the socket's directory's fd) and make D in
           it, mode 0700, under a fresh name (``EEXIST``: another name, up to
           :data:`ATTEMPTS`);
        2. open D (:func:`open_own_dir`), check by ``fstat`` that it is ours
           (:func:`_owner_only`) and set it to 0700 (a umask may have taken
           owner bits off; :func:`set_own_dir_mode`);
        3. create the symlink in D, by its fd.

        ``None`` when any step fails. A D that could not be opened or failed
        the check may be someone else's (put at the name before the open), so
        it is left alone, contents and all; one that passed the check but
        could not be set to 0700 or given the symlink is removed again."""
        if place is SOCKET_DIR:
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
                return None
            verified = False
            try:
                if not _owner_only(os.fstat(fd)):
                    return None
                verified = True
                set_own_dir_mode(fd, base_fd, name, proc_self_fd)
                link = _make_link(fd, target, link_name)
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


def _make_link(fd: int, target: bytes, link_name: bytes | None) -> bytes | None:
    """``symlinkat(target, fd, name)`` under ``link_name`` or, when ``None``,
    a fresh random name (``EEXIST``: another, up to :data:`ATTEMPTS`); the
    name, or ``None`` when it cannot be created."""
    for _ in range(ATTEMPTS if link_name is None else 1):
        try:
            name = random_part() if link_name is None else link_name
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


# ---------- the central route ----------


class Skip(Enum):
    """Why a route did not run."""

    UNUSABLE = "unusable"
    """The route could not be used (too long, nothing to hold, the means
    refused)."""
    WIDE_REFUSED = "wide-refused"
    """The spelling needed the widened form and the kernel refused it
    (Apple)."""


class Means(Enum):
    """A way of reaching ``R/<name>`` from D's fd."""

    THREAD_CWD = "thread-cwd"
    AT = "at"
    PROC = "proc"


def central_means(name: bytes, dirfd: int, cfg: Config, *, proc_ok: bool) -> list[Means]:
    """The means of the central route that can be tried for ``name``, in
    order, judged as far as they can be before D is made (SPEC: nothing is
    made for means that cannot be used):

    * FreeBSD: ``bindat`` / ``connectat``, when ``R/<name>`` fits a sockaddr;
    * Linux, Apple: a thread's own cwd, when it is not turned off, the
      system may give one (:func:`._sys.thread_cwd_possible`) and
      ``R/<name>`` fits a sockaddr;
    * Linux: /proc, when it can be used and ``/proc/self/fd/<n>/R/<name>``
      fits a sockaddr with the fd number of the socket's directory (the
      exact length is checked again with D's own fd).
    """
    fits = IN_OWN_DIR_LEN + len(name) <= DIRECT_MAX
    means: list[Means] = []
    if IS_FREEBSD:
        if fits:
            means.append(Means.AT)
        return means
    if fits and cfg.thread_cwd and HAS_THREAD_CWD and thread_cwd_possible():
        means.append(Means.THREAD_CWD)
    if proc_ok:
        estimate = _join(_join(cfg.proc_self_fd, str(dirfd).encode()), b"R" * RANDOM_LEN + b"/" + name)
        if len(estimate) <= DIRECT_MAX:
            means.append(Means.PROC)
    return means


def central_route(
    req: Request, directory: bytes, name: bytes, dirfd: int, cfg: Config, *, proc_ok: bool
) -> tuple[socket.socket, Route] | None:
    """The central route (SPEC): D in the first of
    :attr:`Config.own_dir_places` where it can be made (:class:`OwnDir`),
    holding R, a symlink to the realpath of the socket's directory, and the
    bind / connect of ``R/<name>`` from D's fd by the first means that can be
    used (:func:`central_means`). D and R are removed again whatever the
    outcome, so the spelling the kernel keeps (``R/<name>``) names nothing
    afterwards, from any cwd.

    ``None`` (nothing left behind) when no means can be used, D can be made
    nowhere, or every means turned out unusable once tried. A failure of the
    bind / connect itself is the outcome and is raised."""
    means = central_means(name, dirfd, cfg, proc_ok=proc_ok)
    if not means:
        return None
    try:
        target = os.fsencode(os.path.realpath(directory, strict=True))
    except OSError:
        return None
    own = OwnDir.make(cfg.own_dir_places, dirfd, target, proc_self_fd=cfg.proc_self_fd)
    if own is None:
        return None
    try:
        spelling = own.link + b"/" + name
        for m in means:
            if m is Means.AT:
                return at_op(req, own.fd, spelling), Route.AT
            if m is Means.THREAD_CWD:
                done = thread_cwd_op(req, own.fd, spelling)
                if isinstance(done, socket.socket):
                    return done, Route.THREAD_CWD
            else:
                via = proc_spelling(own.fd, spelling, cfg.proc_self_fd)
                if via is not None:
                    return plain_op(req, via), Route.PROC_OWN_DIR
        return None
    finally:
        own.remove()


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
    # Who closes `sock` when the caller stops waiting (an exception such as
    # KeyboardInterrupt out of join): the thread may still be using it, so it
    # is handed over to the thread, which closes it when it ends.
    handover = threading.Lock()
    ended = False
    abandoned = False

    def in_thread() -> None:
        nonlocal ended
        try:
            run_in_thread()
        finally:
            with handover:
                ended = True
                if abandoned:
                    sock.close()

    def run_in_thread() -> None:
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
    try:
        thread.join()
    except BaseException:
        with handover:
            if ended:
                sock.close()
            else:
                abandoned = True
        raise
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
    dirfd: int,
    cfg: Config,
    *,
    proc_ok: bool,
) -> tuple[socket.socket, Route]:
    """A connect the central route did not carry, by the SPEC's fallbacks
    for this system, in order:

    ================================ =========================================
    system                           order
    ================================ =========================================
    FreeBSD                          connectat of the file name on ``dirfd``
    Linux with /proc (``proc_ok``)   /proc → fd-held → symlink
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
        return at_op(req, dirfd, name), Route.AT_DIR
    wide = IS_APPLE and cfg.wide
    if proc_ok:
        via = proc_spelling(dirfd, name, cfg.proc_self_fd)
        if via is not None:
            return plain_op(req, via), Route.PROC_FD
        held = fd_held_route(req, path, dirfd, cfg)
        if isinstance(held, tuple):
            return held
    else:
        rel = Relative.new(path, directory)
        if rel is not None and len(rel.path) <= DIRECT_MAX:
            return rel.run(lambda p: plain_op(req, p)), Route.RELATIVE
        if wide:
            try:
                if len(path) <= WIDE_PATH_MAX:
                    return wide_op(req, path), Route.WIDE
                if rel is not None and len(rel.path) <= WIDE_PATH_MAX:
                    return rel.run(lambda p: wide_op(req, p)), Route.WIDE_RELATIVE
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


def fd_held_route(req: Request, path: bytes, dirfd: int, cfg: Config) -> tuple[socket.socket, Route] | Skip:
    """Connect to ``path`` through D (made as the central route makes it, in
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
        return plain_op(req, via), Route.FD_HELD
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


# ---------- the symlink fallback ----------


def symlink_route(req: Request, base: bytes, path: bytes, limit: int) -> tuple[socket.socket, Route] | Skip:
    """Connect to ``path`` through a symlink ``<base>/sockat.XXXXXXXXXX`` to
    the socket file (``path`` made absolute against the cwd, so the kernel
    resolves the link as it resolves ``path``). A connect follows a symlink
    in the last component, so the spelling's length depends on neither the
    directory nor the file name.

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
                return wide_op(req, link.path), Route.SYMLINK_TO_FILE_WIDE
            except OSError as e:
                if refuses_wide(e):
                    return Skip.WIDE_REFUSED
                raise
        return plain_op(req, link.path), Route.SYMLINK_TO_FILE


def safe_base(base: bytes) -> bool:
    """Whether ``base``, followed through symlinks (macOS's ``/tmp`` is one to
    ``/private/tmp``), is a directory where no other user (root aside) can
    remove or rename an entry, so the symlink cannot be replaced between its
    creation and the connect through it (:func:`only_we_can_replace`).
    ``False`` when it cannot be looked up or is not a directory."""
    try:
        st = os.stat(base)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and only_we_can_replace(st.st_mode, st.st_uid, os.geteuid())


def only_we_can_replace(mode: int, owner: int, us: int) -> bool:
    """Whether, in a directory of ``mode`` owned by ``owner``, only the user
    ``us`` and root can remove or rename the entries ``us`` creates:

    * **sticky** (``S_ISVTX``, as ``/tmp`` is) **and owned by root or us**: an
      entry can then be removed or renamed only by its owner, the directory's
      owner and root;
    * **owned by us with no write permission for group and others**
      (``mode & 0o022 == 0``, as a private ``TMPDIR`` is): nobody else can
      create, remove or rename entries there at all.
    """
    sticky = 0o1000
    group_or_other_write = 0o022
    ours = owner in (us, 0)
    return (bool(mode & sticky) and ours) or (owner == us and mode & group_or_other_write == 0)


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
