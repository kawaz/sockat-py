"""The places sockat makes its short spellings in (the hub SPEC-routes'
"B の作り方", "実体の置き場", "D と E"):

* B, a real directory per user in a place for temporary files, kept from
  process to process (:class:`Base`);
* D, a directory in B per (process, B), named at random and held by an fd
  with a shared ``flock`` for as long as the process lives (:class:`Dir`);
* E, a symlink, in D (or, for the default connect, right in B).

What is read from the environment is in :class:`Config`, so that the tests
can give their own; what the process has made is in :class:`State`.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import itertools
import os
import secrets
import stat
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from . import _path
from ._sys import IS_APPLE, IS_LINUX

RANDOM_LEN = 10
"""Length of a random name: D, a random B, E of thread-cwd / at and of the
default connect's link-file."""

ATTEMPTS = 16
"""How many random names are drawn before giving up on making something."""

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def random_name(prefix: bytes = b"") -> bytes:
    """``prefix`` and :data:`RANDOM_LEN` random letters and digits."""
    return prefix + "".join(secrets.choice(_ALPHABET) for _ in range(RANDOM_LEN)).encode()


class Kind(Enum):
    """Where a B is, by the diagnostic name the SPEC gives the spelling
    through it (``base`` of ``_ex``) when it is spelled by its real path."""

    TMP = "tmp"
    RUNTIME = "runtime"
    TMPDIR = "tmpdir"
    SOCKDIR = "sockdir"
    BASE = "given"


@dataclass(frozen=True)
class Place:
    """One of the places for B tried in order (``/tmp``,
    ``$XDG_RUNTIME_DIR``, ``$TMPDIR``), and the name B has there."""

    kind: Kind
    path: bytes
    name: bytes


def default_places(uid: int, xdg_runtime_dir: bytes | None, tmpdir: bytes | None) -> tuple[Place, ...]:
    """The SPEC's places for B in order: ``/tmp`` (``sockat-<uid>``),
    ``$XDG_RUNTIME_DIR`` (``sockat``), ``$TMPDIR`` (``sockat-<uid>``). An
    environment variable that is unset, empty or not absolute is left out
    (a relative one would make the place depend on the cwd)."""
    own = b"sockat-%d" % uid
    places = [Place(Kind.TMP, b"/tmp", own)]
    if xdg_runtime_dir and xdg_runtime_dir.startswith(b"/"):
        places.append(Place(Kind.RUNTIME, xdg_runtime_dir, b"sockat"))
    if tmpdir and tmpdir.startswith(b"/"):
        places.append(Place(Kind.TMPDIR, tmpdir, own))
    return tuple(places)


BIND_FOLLOWS = IS_APPLE or sys.platform.startswith("netbsd")
"""Whether a bind was measured to follow a symlink in the last component
(macOS and NetBSD; hub SPEC-routes, measured 2026-11-19)."""

BIND_DOES_NOT_FOLLOW = IS_LINUX or sys.platform.startswith(("freebsd", "openbsd", "sunos"))
"""Whether a bind was measured not to follow it (Linux / Android, FreeBSD,
OpenBSD, illumos): a bind does not try link-file there."""


class LinkFile(Enum):
    """Whether a bind tries link-file, and how it tells the outcome."""

    FOLLOWS = "follows"
    """Measured to follow the symlink: tried; ``resolve_for_bind`` uses it."""
    TOLD_AT_RUN_TIME = "told at run time"
    """Not measured: tried by a bind, which tells from a failure whether the
    kernel followed the symlink; ``resolve_for_bind`` does not use it."""
    NOT_TRIED = "not tried"
    """Measured not to follow it: not tried."""


def default_link_file() -> LinkFile:
    if BIND_FOLLOWS:
        return LinkFile.FOLLOWS
    if BIND_DOES_NOT_FOLLOW:
        return LinkFile.NOT_TRIED
    return LinkFile.TOLD_AT_RUN_TIME


@dataclass(frozen=True)
class Config:
    """What the routes read from the environment, given explicitly so the
    tests can vary it without touching the process environment."""

    places: tuple[Place, ...]
    """The places for B, in the order tried (:func:`default_places`)."""
    uid: int = field(default_factory=os.getuid)
    proc: bytes | None = b"/proc" if IS_LINUX else None
    """Where /proc is (Linux), ``None`` where there is none. The tests point
    it at a path that does not exist to stand in for a system without
    /proc."""
    wide: bool = IS_APPLE
    """Whether a connect tries the widened ``sockaddr_un`` (Apple)."""
    thread_cwd: bool = IS_LINUX or IS_APPLE
    """Whether a thread's own cwd is tried at all (Linux, Apple)."""
    link_file: LinkFile = field(default_factory=default_link_file)
    """Whether a bind tries link-file (:class:`LinkFile`)."""

    @classmethod
    def from_env(cls) -> Config:
        uid = os.getuid()
        return cls(places=default_places(uid, os.environb.get(b"XDG_RUNTIME_DIR"), os.environb.get(b"TMPDIR")), uid=uid)


# ---------- opening directories ----------

_APPLE_O_SEARCH = 0x40000000 | os.O_DIRECTORY
"""``O_SEARCH`` of Apple's ``<sys/fcntl.h>``: ``O_EXEC (0x40000000) |
O_DIRECTORY``.

Design rationale: written out rather than taken from ``os.O_SEARCH``, which
some Python builds for macOS do not have (the python-build-standalone 3.11 of
the CI lacks it; the module defines it only when the build saw the macro).
The value is the system's, not Python's, so it does not depend on the
build."""


def open_dir(directory: bytes, *, nofollow: bool = False, dir_fd: int | None = None) -> int:
    """Open ``directory`` to hold it by an fd for the ``*at`` calls (the
    SPEC-routes' "ディレクトリの開き方"): Linux ``O_PATH | O_DIRECTORY``,
    Apple ``O_SEARCH`` (``O_RDONLY`` where the kernel answers ``EISDIR`` /
    ``EINVAL`` to it), elsewhere ``O_RDONLY | O_DIRECTORY``; with
    ``O_NOFOLLOW`` when ``nofollow``. The fd is close-on-exec (as
    :func:`os.open` makes every fd)."""
    extra = os.O_NOFOLLOW if nofollow else 0
    if sys.platform == "linux" or sys.platform == "android":
        return os.open(directory, os.O_PATH | os.O_DIRECTORY | extra, dir_fd=dir_fd)
    if IS_APPLE:
        return search_or_read_only(
            lambda: os.open(directory, _APPLE_O_SEARCH | extra, dir_fd=dir_fd),
            lambda: os.open(directory, os.O_RDONLY | os.O_DIRECTORY | extra, dir_fd=dir_fd),
        )
    return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | extra, dir_fd=dir_fd)


def search_or_read_only(search: Callable[[], int], read_only: Callable[[], int]) -> int:
    """``search()`` (``O_SEARCH``), unless it fails the way a kernel that
    does not take ``O_SEARCH`` on a directory fails (``EISDIR`` or
    ``EINVAL``); then ``read_only()``. Any other error is the outcome."""
    try:
        return search()
    except OSError as e:
        if e.errno in (errno.EISDIR, errno.EINVAL):
            return read_only()
        raise


def _open_own(name: bytes, dir_fd: int) -> int:
    """Open the directory ``name`` in ``dir_fd`` to read and change it (B or
    D, which belong to us), not through a symlink."""
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


# ---------- checks ----------


def _prefixes(real: bytes) -> list[bytes]:
    """``/`` and every directory on the way to the absolute ``real``,
    ``real`` itself last."""
    out = [b"/"]
    walked = b""
    for c in _path.components(real):
        walked += b"/" + c
        out.append(walked)
    return out


def safe_ancestors(real_parent: bytes, uid: int) -> bool:
    """Whether every directory from the root to ``real_parent`` (a real
    path) is owned by root or ``uid`` and writable by nobody else, or is
    sticky (SPEC-routes: OpenSSH's StrictModes; an ancestor others can write
    to lets them swap the parent)."""
    for p in _prefixes(real_parent):
        try:
            st = os.lstat(p)
        except OSError:
            return False
        if not stat.S_ISDIR(st.st_mode):
            return False
        owned = st.st_uid in (0, uid) and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if not owned and not st.st_mode & stat.S_ISVTX:
            return False
    return True


def searchable_by_others(real: bytes) -> bool:
    """Whether every directory from the root to ``real`` lets others search
    it (``o+x``): a spelling through ``real`` can be followed by another
    uid."""
    for p in _prefixes(real):
        try:
            st = os.stat(p)
        except OSError:
            return False
        if not st.st_mode & stat.S_IXOTH:
            return False
    return True


_SHARED_TMP = (b"/tmp", b"/var/tmp")
"""The places a private ``/tmp`` (systemd's ``PrivateTmp`` and the like)
hides from the other side."""


def _shared_tmp_spellings() -> list[list[bytes]]:
    """``/tmp`` and ``/var/tmp`` as components, and their realpaths
    (``/private/tmp`` on macOS)."""
    out = [_path.components(t) for t in _SHARED_TMP]
    for t in _SHARED_TMP:
        with contextlib.suppress(OSError):
            real = _path.components(os.fsencode(os.path.realpath(t, strict=True)))
            if real not in out:
                out.append(real)
    return out


def goes_through_shared_tmp(path: bytes) -> bool:
    """Whether ``path`` goes through ``/tmp`` or ``/var/tmp``: it is one of
    them or below one, component by component, or below their realpath."""
    comps = _path.components(path)
    return any(comps[: len(t)] == t for t in _shared_tmp_spellings())


def _is_ours(st: os.stat_result, uid: int) -> bool:
    return stat.S_ISDIR(st.st_mode) and st.st_uid == uid and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


# ---------- B, D, E ----------


class Dir:
    """D: a directory in B named at random, held by an fd with a shared
    ``flock`` for as long as the process lives (``sweep`` takes a D whose
    lock it can make exclusive as one whose owner is gone)."""

    def __init__(self, base: Base, name: bytes, fd: int) -> None:
        self.base = base
        self.name = name
        self.fd = fd
        self.closed = False
        self._lock = threading.Lock()
        self._counter = itertools.count(1)

    def link(self, target: bytes) -> bytes | None:
        """A symlink E to ``target`` named by a counter (unique in the
        process, D being unique to it), or ``None`` when it cannot be
        made."""
        for _ in range(ATTEMPTS):
            with self._lock:
                name = _base36(next(self._counter))
            made = self._symlink(target, name)
            if made is not False:
                return name if made else None
        return None

    def random_link(self, target: bytes) -> bytes | None:
        """A symlink E to ``target`` named at random (thread-cwd / at), or
        ``None`` when it cannot be made."""
        for _ in range(ATTEMPTS):
            name = random_name()
            made = self._symlink(target, name)
            if made is not False:
                return name if made else None
        return None

    def _symlink(self, target: bytes, name: bytes) -> bool | None:
        """``True`` when made, ``False`` when the name is taken, ``None`` on
        any other failure."""
        if self.closed:
            return None
        try:
            os.symlink(target, name, dir_fd=self.fd)
        except FileExistsError:
            return False
        except OSError:
            return None
        return True

    def unlink(self, name: bytes) -> None:
        """Remove E ``name`` (gone already counts as removed)."""
        if self.closed:
            return
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=self.fd)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            os.close(self.fd)


def _base36(n: int) -> bytes:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out.encode()


class Base:
    """B: a real directory sockat keeps in a place, held by an fd."""

    def __init__(self, kind: Kind, real: bytes, fd: int, place: bytes) -> None:
        self.kind = kind
        self.real = real
        """B's real path (the real-path spelling's start)."""
        self.fd = fd
        self.cross_uid = searchable_by_others(real)
        """Whether another uid can follow a spelling through B's real path
        (every directory on the way and B itself let others search them)."""
        self.private_tmp_ok = not goes_through_shared_tmp(place) and not goes_through_shared_tmp(real)
        """Whether a spelling through B's real path can be followed with a
        private ``/tmp`` on either side: neither the place as given (before
        its realpath) nor B's real path goes through ``/tmp`` /
        ``/var/tmp``."""
        self.closed = False
        self._d: Dir | None = None
        self._d_tried = False
        self._lock = threading.Lock()

    def proc(self, proc: bytes, pid: int) -> bytes:
        """procB: ``<proc>/<pid>/fd/<B's fd>``."""
        return b"%s/%d/fd/%d" % (proc, pid, self.fd)

    def proc_usable(self, proc: bytes, pid: int) -> bool:
        """Whether procB names B: ``stat`` of it gives the ``(st_dev,
        st_ino)`` of B's fd (a /proc of another PID namespace or mount does
        not)."""
        try:
            via = os.stat(self.proc(proc, pid))
            st = os.fstat(self.fd)
        except OSError:
            return False
        return (via.st_dev, via.st_ino) == (st.st_dev, st.st_ino)

    def dir(self, uid: int) -> Dir | None:
        """D of this process in B, made on the first call; ``None`` when it
        cannot be made (not tried again)."""
        with self._lock:
            if not self._d_tried and not self.closed:
                self._d_tried = True
                self._d = make_dir(self, uid)
            return self._d

    def random_link(self, target: bytes) -> bytes | None:
        """A symlink E to ``target`` right in B, named at random (the
        default connect's link-file), or ``None``."""
        for _ in range(ATTEMPTS):
            name = random_name()
            try:
                os.symlink(target, name, dir_fd=self.fd)
            except FileExistsError:
                continue
            except OSError:
                return None
            return name
        return None

    def unlink(self, name: bytes) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=self.fd)

    def close(self) -> None:
        if self._d is not None:
            self._d.close()
        if not self.closed:
            self.closed = True
            os.close(self.fd)


def make_dir(base: Base, uid: int) -> Dir | None:
    """Make D in ``base`` (SPEC-routes "D の作り方"): ``mkdirat`` a random
    name (another one on ``EEXIST``, :data:`ATTEMPTS` at most), open it
    ``O_DIRECTORY | O_NOFOLLOW``, check it is a real directory of ours, mode
    0711, and hold a shared ``flock`` on the fd.

    Once locked, the name is looked up again and must still be the directory
    held: a ``sweep`` of another process may have taken the lock and removed
    the directory between the ``mkdirat`` and the ``flock`` (it sees no
    lock yet), and then another name is drawn."""
    for _ in range(ATTEMPTS):
        name = random_name()
        try:
            os.mkdir(name, 0o700, dir_fd=base.fd)
        except FileExistsError:
            continue
        except OSError:
            return None
        try:
            fd = _open_own(name, base.fd)
        except FileNotFoundError:
            continue
        except OSError:
            # The umask took the owner's r or x off: remove the empty
            # directory just made.
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=base.fd)
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
                os.close(fd)
                return None
            os.fchmod(fd, 0o711)
            fcntl.flock(fd, fcntl.LOCK_SH)
            now = os.stat(name, dir_fd=base.fd, follow_symlinks=False)
        except FileNotFoundError:
            os.close(fd)
            continue
        except OSError:
            os.close(fd)
            return None
        if (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
            os.close(fd)
            continue
        return Dir(base, name, fd)
    return None


class BaseError(Enum):
    """Why ``Options.base`` cannot be B (SPEC: the errors of
    ``Options.base``)."""

    NO_DIRECTORY = errno.ENOENT
    NOT_A_DIRECTORY = errno.ENOTDIR
    REFUSED = errno.EPERM


def _base_error(e: BaseError, path: bytes) -> OSError:
    return OSError(e.value, os.strerror(e.value), os.fsdecode(path))


def make_base(
    kind: Kind,
    parent: bytes,
    name: bytes | None,
    uid: int,
    *,
    checked: set[bytes],
    strict: bool = False,
    cross_uid: bool = False,
) -> Base | None:
    """Make (or take the existing) B ``name`` in ``parent`` (SPEC-routes "B
    の作り方"), or ``None`` when it cannot be made. ``name`` ``None`` draws a
    random ``.sockat.XXXXXXXXXX`` (B in the socket's directory).

    1. ``parent`` is resolved to its real path, and every directory from the
       root to it is checked (:func:`safe_ancestors`), once per process
       (``checked`` holds the real parents that passed).
    2. ``parent`` is opened ``O_DIRECTORY | O_NOFOLLOW`` and B is made in it
       with ``mkdirat`` (0700), opened ``O_NOFOLLOW``, checked to be a real
       directory of ours, then made 0711.
    3. On ``EEXIST`` the one there is checked the same way (a real directory
       of ours that nobody else can write to). Where it fails, a random name
       ``<name>.XXXXXXXXXX`` is tried instead.

    With ``cross_uid``, a parent another uid cannot search its way to is not
    used.

    ``strict`` (``Options.base``): nothing else is tried, and what fails is
    raised: ``ENOENT`` when ``parent`` is missing, ``ENOTDIR`` when it or B
    is not a directory, ``EPERM`` when a check fails, and the error of the
    ``mkdirat`` otherwise.
    """
    try:
        real_parent = os.fsencode(os.path.realpath(parent, strict=True))
        if not stat.S_ISDIR(os.stat(real_parent).st_mode):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), os.fsdecode(parent))
    except FileNotFoundError:
        if strict:
            raise _base_error(BaseError.NO_DIRECTORY, parent) from None
        return None
    except NotADirectoryError:
        if strict:
            raise _base_error(BaseError.NOT_A_DIRECTORY, parent) from None
        return None
    except OSError:
        if strict:
            raise
        return None
    if real_parent not in checked:
        if not safe_ancestors(real_parent, uid):
            if strict:
                raise _base_error(BaseError.REFUSED, parent)
            return None
        checked.add(real_parent)
    if cross_uid and not searchable_by_others(real_parent):
        if strict:
            raise _base_error(BaseError.REFUSED, parent)
        return None
    try:
        pfd = open_dir(real_parent, nofollow=True)
    except OSError:
        if strict:
            raise
        return None
    try:
        made = _make_in(kind, parent, real_parent, pfd, name, uid, strict=strict)
    finally:
        os.close(pfd)
    if made is not None and cross_uid and not made.cross_uid:
        if strict:
            made.close()
            raise _base_error(BaseError.REFUSED, made.real)
        made.close()
        return None
    return made


def _make_in(
    kind: Kind, place: bytes, real_parent: bytes, pfd: int, name: bytes | None, uid: int, *, strict: bool
) -> Base | None:
    """B ``name`` (made, or the one there when it passes the checks) in the
    parent held by ``pfd``; where the one there fails them, or ``name`` is
    ``None``, a fresh random name (``<name>.XXXXXXXXXX`` or
    ``.sockat.XXXXXXXXXX``)."""
    if name is not None:
        made = _mkdir_new(kind, place, real_parent, pfd, name, uid, strict=strict)
        if made is not _Taken.TAKEN:
            return made
        got = _take_existing(kind, place, real_parent, pfd, name, uid)
        if isinstance(got, Base):
            return got
        if strict:
            raise _base_error(got, _path_join(real_parent, name))
        prefix = name + b"."
    else:
        prefix = b".sockat."
    for _ in range(ATTEMPTS):
        made = _mkdir_new(kind, place, real_parent, pfd, random_name(prefix), uid, strict=strict)
        if made is not _Taken.TAKEN:
            return made
    return None


class _Taken(Enum):
    TAKEN = "taken"


def _mkdir_new(
    kind: Kind, place: bytes, real_parent: bytes, pfd: int, name: bytes, uid: int, *, strict: bool
) -> Base | _Taken | None:
    """B made fresh at ``name`` (``mkdirat`` 0700, opened ``O_NOFOLLOW``,
    checked to be a real directory of ours, then 0711); :attr:`_Taken.TAKEN`
    when the name is taken; ``None`` on any other failure (raised when
    ``strict``)."""
    try:
        os.mkdir(name, 0o700, dir_fd=pfd)
    except FileExistsError:
        return _Taken.TAKEN
    except OSError:
        if strict:
            raise
        return None
    try:
        fd = _open_own(name, pfd)
    except OSError:
        # The umask took the owner's r or x off: remove the empty directory
        # just made, and go on to the next place (Options.base: the error).
        with contextlib.suppress(OSError):
            os.rmdir(name, dir_fd=pfd)
        if strict:
            raise
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
            raise _base_error(BaseError.REFUSED, _path_join(real_parent, name))
        os.fchmod(fd, 0o711)
    except OSError:
        os.close(fd)
        if strict:
            raise
        return None
    return Base(kind, _path_join(real_parent, name), fd, place)


def _take_existing(kind: Kind, place: bytes, real_parent: bytes, pfd: int, name: bytes, uid: int) -> Base | BaseError:
    """The B already at ``name``, checked (a real directory of ours that
    nobody else can write to), or why it cannot be used."""
    try:
        fd = _open_own(name, pfd)
    except OSError:
        # Which check failed, the same on every system (an O_NOFOLLOW open of
        # a symlink answers ELOOP, EMLINK, EFTYPE or ENOTDIR by the system): a
        # symlink is not a real directory (refused), anything else that is not
        # a directory is not one.
        try:
            st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
        except OSError:
            return BaseError.REFUSED
        if not stat.S_ISLNK(st.st_mode) and not stat.S_ISDIR(st.st_mode):
            return BaseError.NOT_A_DIRECTORY
        return BaseError.REFUSED
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return BaseError.REFUSED
    if not _is_ours(st, uid):
        os.close(fd)
        return BaseError.REFUSED
    return Base(kind, _path_join(real_parent, name), fd, place)


def _path_join(a: bytes, b: bytes) -> bytes:
    return a + b if a.endswith(b"/") else a + b"/" + b


# ---------- the process's state ----------


class State:
    """What this process has made: its B among the places (one, and one
    more for ``usable_cross_uid`` where that is another), B in socket
    directories and at ``Options.base``, each with its D once made."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.auto: Base | None = None
        self.cross: Base | None = None
        self.others: dict[tuple[Kind, bytes], Base] = {}
        self.checked: set[bytes] = set()

    def bases(self) -> list[Base]:
        out = [b for b in (self.auto, self.cross) if b is not None]
        return out + [b for b in self.others.values() if b not in out]

    def abandon(self) -> None:
        """Close every fd held (a child after a fork: its B and D are the
        parent's)."""
        for b in self.bases():
            b.close()
        self.auto = None
        self.cross = None
        self.others = {}


_state = State()


def state() -> State:
    """The process's :class:`State`."""
    return _state


def _after_fork_in_child() -> None:
    global _state
    old = _state
    _state = State()
    old.abandon()


os.register_at_fork(after_in_child=_after_fork_in_child)


@dataclass
class Env:
    """The configuration and the state a call works with."""

    config: Config
    state: State

    @classmethod
    def current(cls) -> Env:
        return cls(Config.from_env(), state())

    def auto_base(self) -> Base | None:
        """The process's B: the first place where it can be made (kept for
        the life of the process), or ``None``."""
        st = self.state
        with st.lock:
            if st.auto is None or st.auto.closed:
                st.auto = None
                for place in self.config.places:
                    b = make_base(place.kind, place.path, place.name, self.config.uid, checked=st.checked)
                    if b is not None:
                        st.auto = b
                        break
            return st.auto

    def cross_base(self) -> Base | None:
        """B another uid can follow a spelling through: the first place that
        lets others search their way to it, the process's B when that one
        does."""
        st = self.state
        with st.lock:
            if st.cross is None or st.cross.closed:
                st.cross = None
                auto = st.auto
                for place in self.config.places:
                    if auto is not None and auto.kind is place.kind and auto.cross_uid:
                        st.cross = auto
                        break
                    b = make_base(
                        place.kind, place.path, place.name, self.config.uid, checked=st.checked, cross_uid=True
                    )
                    if b is not None:
                        st.cross = b
                        break
            return st.cross

    def sockdir_base(self, real_dir: bytes) -> Base | None:
        """B in the socket's directory (the last place), one per directory
        for the process."""
        st = self.state
        key = (Kind.SOCKDIR, real_dir)
        with st.lock:
            b = st.others.get(key)
            if b is None or b.closed:
                b = make_base(Kind.SOCKDIR, real_dir, None, self.config.uid, checked=st.checked)
                if b is None:
                    st.others.pop(key, None)
                    return None
                st.others[key] = b
            return b

    def given_base(self, path: bytes, *, cross_uid: bool) -> Base:
        """B at ``Options.base``, made on the first use (checked beforehand
        by :func:`check_given`); what fails is raised (``ENOENT``,
        ``ENOTDIR``, ``EPERM``, the ``mkdirat``'s error)."""
        parent, name = _path.split(path)
        st = self.state
        real_parent = os.fsencode(os.path.realpath(parent, strict=True))
        key = (Kind.BASE, _path_join(real_parent, name))
        with st.lock:
            b = st.others.get(key)
            if b is None or b.closed:
                made = make_base(Kind.BASE, parent, name, self.config.uid, checked=st.checked, strict=True)
                assert made is not None
                b = made
                st.others[key] = b
            if cross_uid and not b.cross_uid:
                raise _base_error(BaseError.REFUSED, path)
            return b

    def real_len_guess(self, *, cross_uid: bool, given: bytes | None) -> int | None:
        """The length of the real path of the B a spelling would go through,
        known (B made already, or ``Options.base``) or guessed as the
        shortest the places allow (``None`` when no place resolves), so that
        B is not made for a spelling that cannot fit."""
        if given is not None:
            parent, name = _path.split(given)
            try:
                return len(_path_join(os.fsencode(os.path.realpath(parent, strict=True)), name))
            except OSError:
                return None
        made = self.state.cross if cross_uid else self.state.auto
        if made is not None and not made.closed:
            return len(made.real)
        lengths = []
        for place in self.config.places:
            try:
                real = os.fsencode(os.path.realpath(place.path, strict=True))
            except OSError:
                continue
            lengths.append(len(_path_join(real, place.name)))
        return min(lengths) if lengths else None


def check_given(path: bytes, uid: int, checked: set[bytes], *, cross_uid: bool) -> None:
    """Check ``Options.base`` before any route (SPEC-routes "B の作り方"),
    making nothing: an absolute path ending with a name (``ValueError``
    otherwise), its parent there (``ENOENT``) and a directory (``ENOTDIR``),
    every directory from the root to it safe (``EPERM``), and B itself, when
    it is there, a real directory of ours nobody else can write to
    (``ENOTDIR`` / ``EPERM``). With ``cross_uid``, every directory on the
    way and B itself must let others search them (``EPERM``)."""
    if not path.startswith(b"/"):
        raise ValueError("Options.base must be an absolute path")
    parent, name = _path.split(path)
    try:
        real_parent = os.fsencode(os.path.realpath(parent, strict=True))
        if not stat.S_ISDIR(os.stat(real_parent).st_mode):
            raise _base_error(BaseError.NOT_A_DIRECTORY, path)
    except FileNotFoundError:
        raise _base_error(BaseError.NO_DIRECTORY, path) from None
    except NotADirectoryError:
        raise _base_error(BaseError.NOT_A_DIRECTORY, path) from None
    if real_parent not in checked:
        if not safe_ancestors(real_parent, uid):
            raise _base_error(BaseError.REFUSED, path)
        checked.add(real_parent)
    if cross_uid and not searchable_by_others(real_parent):
        raise _base_error(BaseError.REFUSED, path)
    b = _path_join(real_parent, name)
    try:
        st = os.lstat(b)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise _base_error(BaseError.REFUSED, path)
    if not stat.S_ISDIR(st.st_mode):
        raise _base_error(BaseError.NOT_A_DIRECTORY, path)
    if not _is_ours(st, uid):
        raise _base_error(BaseError.REFUSED, path)
    if cross_uid and not st.st_mode & stat.S_IXOTH:
        raise _base_error(BaseError.REFUSED, path)


# ---------- sweep ----------


def sweep(env: Env) -> None:
    """Remove the D of the processes gone (whose lock can be made exclusive)
    from B of every automatic place (``sockat-<uid>`` / ``sockat``; B is not
    made where it is not). Nothing else in B is touched (SPEC "sweep")."""
    for place in env.config.places:
        _sweep_at(place.kind, place.path, place.name, env.config.uid)


def sweep_given(env: Env, path: bytes) -> None:
    """:func:`sweep` for the B given as ``Options.base``, checked as
    :func:`check_given` checks it (nothing is made)."""
    check_given(path, env.config.uid, set(), cross_uid=False)
    parent, name = _path.split(path)
    _sweep_at(Kind.BASE, parent, name, env.config.uid)


def _sweep_at(kind: Kind, place: bytes, name: bytes, uid: int) -> None:
    try:
        real_parent = os.fsencode(os.path.realpath(place, strict=True))
        pfd = open_dir(real_parent, nofollow=True)
    except OSError:
        return
    try:
        got = _take_existing(kind, place, real_parent, pfd, name, uid)
    finally:
        os.close(pfd)
    if not isinstance(got, Base):
        return
    try:
        _sweep_base(got.fd, uid)
    finally:
        got.close()


def _sweep_base(bfd: int, uid: int) -> None:
    """Remove each D in B (``bfd``) whose lock can be made exclusive, with
    the E in it. A D holding anything but symlinks is left; the E right in B
    (the default connect's) are not touched."""
    with os.scandir(bfd) as entries:
        names = [os.fsencode(e.name) for e in entries if e.is_dir(follow_symlinks=False)]
    for name in names:
        try:
            dfd = _open_own(name, bfd)
        except OSError:
            continue
        try:
            st = os.fstat(dfd)
            if st.st_uid != uid:
                continue
            try:
                fcntl.flock(dfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            now = os.stat(name, dir_fd=bfd, follow_symlinks=False)
            if (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
                continue
            with os.scandir(dfd) as inside:
                entries_in = [(os.fsencode(e.name), e.is_symlink()) for e in inside]
            if not all(is_link for _, is_link in entries_in):
                continue
            for link, _ in entries_in:
                with contextlib.suppress(OSError):
                    os.unlink(link, dir_fd=dfd)
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=bfd)
        except OSError:
            continue
        finally:
            os.close(dfd)
