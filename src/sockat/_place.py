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
import re
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
"""Length of a random name: D, B in a socket's directory, E of thread-cwd / at and of the
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
    # A place whose real path is that of an earlier one ($TMPDIR=/tmp) is the
    # same place: tried once.
    out: list[Place] = []
    seen: set[tuple[bytes, bytes]] = set()
    for place in places:
        key = (os.fsencode(os.path.realpath(place.path)), place.name)
        if key not in seen:
            seen.add(key)
            out.append(place)
    return tuple(out)


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
    uid: int = field(default_factory=os.geteuid)
    """The effective uid: the owner of what the process makes, and the
    ``<uid>`` of ``sockat-<uid>``."""
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
        uid = os.geteuid()
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
    path) is owned by root or ``uid``, and either writable by nobody else
    (group included) or sticky (SPEC-routes: OpenSSH's StrictModes; an
    ancestor others can write to lets them swap the parent, and the owner of
    a sticky one can still swap what is in it)."""
    for p in _prefixes(real_parent):
        try:
            st = os.lstat(p)
        except OSError:
            return False
        if not stat.S_ISDIR(st.st_mode) or st.st_uid not in (0, uid):
            return False
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not st.st_mode & stat.S_ISVTX:
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
        self._shared: dict[bytes, bytes] = {}
        """The default bind's link-dir E, one per socket directory's realpath
        (SPEC-routes "link-dir"): the target and E's name."""
        self._shared_lock = threading.Lock()

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

    def shared_link(self, target: bytes) -> tuple[bytes, bool] | None:
        """The default bind's E to ``target`` (a socket directory's realpath),
        one per target and used again: ``(name, made now)``, or ``None`` when
        it cannot be made. An E used again is checked first (``readlinkat``
        from D's fd): gone, or pointing elsewhere, it is let go and another
        made."""
        with self._shared_lock:
            name = self._shared.get(target)
            if name is not None:
                try:
                    now: bytes | None = os.readlink(name, dir_fd=self.fd)
                except OSError:
                    now = None
                if now == target:
                    return name, False
                del self._shared[target]
            made = self.link(target)
            if made is None:
                return None
            self._shared[target] = made
            return made, True

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

    def gone(self) -> bool:
        """Whether D was removed (by an old systemd-tmpfiles, macOS's cleaning
        of ``$TMPDIR``, by hand) since it was made: its fd's ``st_nlink`` is
        0, or its name no longer names it."""
        return _gone(self.fd, self.name, self.base.fd)

    def unlink_or_raise(self, name: bytes) -> None:
        """Remove E ``name`` for the caller's ``cleanup()``: gone already is
        removed; any other failure is raised."""
        if self.closed:
            return
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=self.fd)

    def unlink(self, name: bytes) -> None:
        """Remove E ``name``. A failure is passed over in silence (SPEC-routes:
        cleaning up E changes neither the call's outcome nor its error)."""
        if self.closed:
            return
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=self.fd)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            os.close(self.fd)


def _gone(fd: int, name: bytes, dir_fd: int | None) -> bool:
    """Whether the directory held by ``fd`` is no longer at ``name``: its
    ``st_nlink`` is 0 (removed), or ``lstat`` of the name gives another
    ``(st_dev, st_ino)`` or nothing."""
    try:
        held = os.fstat(fd)
        if held.st_nlink == 0:
            return True
        now = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return True
    return (now.st_dev, now.st_ino) != (held.st_dev, held.st_ino)


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
        parent = real.rsplit(b"/", 1)[0] or b"/"
        self._way_x = searchable_by_others(parent)
        """Whether others can search every directory on the way to B, judged
        when B was first held and kept (SPEC-routes)."""
        try:
            self._self_x = bool(os.fstat(fd).st_mode & stat.S_IXOTH)
        except OSError:
            self._self_x = False
        self.private_tmp_ok = not goes_through_shared_tmp(place) and not goes_through_shared_tmp(real)
        """Whether a spelling through B's real path can be followed with a
        private ``/tmp`` on either side: neither the place as given (before
        its realpath) nor B's real path goes through ``/tmp`` /
        ``/var/tmp``."""
        self.closed = False
        self._d: Dir | None = None
        self._lock = threading.Lock()

    @property
    def cross_uid(self) -> bool:
        """Whether another uid can follow a spelling through B's real path:
        every directory on the way and B itself let others search them. B
        itself found without ``o+x`` is looked at again (``fstat`` of its fd)
        each time: two processes making B at once, one may open it between
        the other's ``mkdirat`` (0700) and ``fchmodat`` (0711)."""
        if self._way_x and not self._self_x:
            with contextlib.suppress(OSError):
                self._self_x = bool(os.fstat(self.fd).st_mode & stat.S_IXOTH)
        return self._way_x and self._self_x

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

    def gone(self) -> bool:
        """Whether B was removed since it was made (as :meth:`Dir.gone`)."""
        return _gone(self.fd, self.real, None)

    def dir(self, uid: int) -> Dir | None:
        """D of this process in B, made on the first call that can; ``None``
        when it cannot be made now (that call goes without the routes using
        D, and a later call tries again). A D removed since is let go and
        made again."""
        with self._lock:
            if self._d is not None and self._d.gone():
                self._d.close()
                self._d = None
            if self._d is None and not self.closed:
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
        """Remove E ``name`` right in B, a failure passed over in silence (as
        :meth:`Dir.unlink`)."""
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=self.fd)

    def close(self) -> None:
        if self._d is not None:
            self._d.close()
        if not self.closed:
            self.closed = True
            os.close(self.fd)


class _Taken(Enum):
    TAKEN = "taken"


def make_own_dir(pfd: int, name: bytes, uid: int) -> int | _Taken | OSError:
    """Make the directory ``name`` in ``pfd`` for B or D (SPEC-routes "B の作り
    方" 3-4, "D の作り方" 1-2): ``mkdirat`` 0700, ``fchmodat`` 0711 by the
    name (giving back what the umask took), then open it ``O_DIRECTORY |
    O_NOFOLLOW`` and check it is a real directory of ours. The fd, or
    :attr:`_Taken.TAKEN` when the name is taken, or the error; on an error
    after the ``mkdirat`` the empty directory just made is removed."""
    try:
        os.mkdir(name, 0o700, dir_fd=pfd)
    except FileExistsError:
        return _Taken.TAKEN
    except OSError as e:
        return e
    try:
        # Design rationale: chmod by the name, following a symlink (Linux's
        # fchmodat takes no AT_SYMLINK_NOFOLLOW). Once mkdirat succeeded the
        # name cannot be removed or swapped by others (the parent is checked,
        # and even a sticky /tmp keeps others from it), so the fstat of what
        # is opened below (a real directory of ours) is check enough.
        os.chmod(name, 0o711, dir_fd=pfd)
        fd = _open_own(name, pfd)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.rmdir(name, dir_fd=pfd)
        return e
    try:
        st = os.fstat(fd)
    except OSError as e:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.rmdir(name, dir_fd=pfd)
        return e
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.rmdir(name, dir_fd=pfd)
        return OSError(errno.EPERM, os.strerror(errno.EPERM), os.fsdecode(name))
    return fd


def make_dir(base: Base, uid: int) -> Dir | None:
    """Make D in ``base`` (SPEC-routes "D の作り方"): a random name
    (another one on ``EEXIST``, :data:`ATTEMPTS` at most) made by
    :func:`make_own_dir`, then a shared ``flock`` held on its fd.

    Once locked, the name is looked up again and must still be the directory
    held: a ``sweep`` of another process may have taken the lock and removed
    the directory between the ``mkdirat`` and the ``flock`` (it sees no
    lock yet), and then another name is drawn."""
    for _ in range(ATTEMPTS):
        name = random_name()
        fd = make_own_dir(base.fd, name, uid)
        if fd is _Taken.TAKEN:
            continue
        if isinstance(fd, OSError):
            if isinstance(fd, FileNotFoundError):
                continue
            return None
        try:
            st = os.fstat(fd)
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
    occupied: list[bytes] | None = None,
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
       of ours that nobody else can write to). Where it fails, the place is
       taken over by someone else (occupied): ``None``, the B's path added
       to ``occupied`` (SPEC-routes: no other B there; the next place).

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
        made = _make_in(kind, parent, real_parent, pfd, name, uid, strict=strict, occupied=occupied)
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
    kind: Kind,
    place: bytes,
    real_parent: bytes,
    pfd: int,
    name: bytes | None,
    uid: int,
    *,
    strict: bool,
    occupied: list[bytes] | None = None,
) -> Base | None:
    """B ``name`` (made, or the one there when it passes the checks) in the
    parent held by ``pfd``. One there that fails them is taken over by
    someone else: the place cannot be used (its path is added to
    ``occupied``; ``Options.base`` raises). ``name`` ``None`` (the socket's
    directory): a fresh ``.sockat.XXXXXXXXXX``."""
    if name is not None:
        made = _mkdir_new(kind, place, real_parent, pfd, name, uid, strict=strict)
        if made is not _Taken.TAKEN:
            return made
        got = _take_existing(kind, place, real_parent, pfd, name, uid)
        if isinstance(got, Base):
            return got
        if strict:
            raise _base_error(got, _path_join(real_parent, name))
        if occupied is not None:
            occupied.append(_path_join(real_parent, name))
        return None
    for _ in range(ATTEMPTS):
        made = _mkdir_new(kind, place, real_parent, pfd, random_name(b".sockat."), uid, strict=strict)
        if made is not _Taken.TAKEN:
            return made
    return None


def _mkdir_new(
    kind: Kind, place: bytes, real_parent: bytes, pfd: int, name: bytes, uid: int, *, strict: bool
) -> Base | _Taken | None:
    """B made fresh at ``name`` (:func:`make_own_dir`);
    :attr:`_Taken.TAKEN` when the name is taken; ``None`` on any other
    failure (raised when ``strict``)."""
    fd = make_own_dir(pfd, name, uid)
    if fd is _Taken.TAKEN:
        return fd
    if isinstance(fd, OSError):
        if strict:
            raise fd
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
        self.places: dict[tuple[bytes, bytes], Base] = {}
        """B held per place (its real path and B's name there)."""
        self.others: dict[tuple[Kind, bytes], Base] = {}
        self.checked: set[bytes] = set()

    def bases(self) -> list[Base]:
        out: list[Base] = []
        for b in (self.auto, self.cross, *self.places.values(), *self.others.values()):
            if b is not None and b not in out:
                out.append(b)
        return out

    def abandon(self) -> None:
        """Close every fd held (a child after a fork: its B and D are the
        parent's)."""
        for b in self.bases():
            b.close()
        self.auto = None
        self.cross = None
        self.places = {}
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

    def _at_place(self, place: Place, *, cross_uid: bool, occupied: list[bytes] | None = None) -> Base | None:
        """B at ``place``: the one this process holds there already (one B,
        and one D, per place whatever asks for it), or one made now."""
        st = self.state
        key = _place_key(place)
        if key is not None:
            held = _alive(st.places.get(key))
            if held is not None:
                return held if (held.cross_uid or not cross_uid) else None
        b = make_base(
            place.kind,
            place.path,
            place.name,
            self.config.uid,
            checked=st.checked,
            cross_uid=cross_uid,
            occupied=occupied,
        )
        if b is not None and key is not None:
            st.places[key] = b
        return b

    def auto_base(self, occupied: list[bytes] | None = None) -> Base | None:
        """The process's B: the first place where it can be had, kept for the
        life of the process once had (a place where it could not be made is
        tried again by a later call), or ``None``."""
        st = self.state
        with st.lock:
            st.auto = _alive(st.auto)
            if st.auto is None:
                for place in self.config.places:
                    b = self._at_place(place, cross_uid=False, occupied=occupied)
                    if b is not None:
                        st.auto = b
                        break
            return st.auto

    def cross_base(self, occupied: list[bytes] | None = None) -> Base | None:
        """B another uid can follow a spelling through: the first place that
        lets others search their way to it and to B (judged when B was
        made); the process's B itself when that one does."""
        st = self.state
        with st.lock:
            st.cross = _alive(st.cross)
            if st.cross is None:
                for place in self.config.places:
                    b = self._at_place(place, cross_uid=True, occupied=occupied)
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
            b = _alive(st.others.get(key))
            if b is None:
                b = make_base(Kind.SOCKDIR, real_dir, None, self.config.uid, checked=st.checked)
                if b is None:
                    st.others.pop(key, None)
                    return None
                st.others[key] = b
            return b

    def held_given(self, path: bytes) -> Base | None:
        """The B at ``Options.base`` this process holds (still there), or
        ``None``."""
        parent, name = _path.split(path)
        try:
            key = (Kind.BASE, _path_join(os.fsencode(os.path.realpath(parent, strict=True)), name))
        except OSError:
            return None
        with self.state.lock:
            b = _alive(self.state.others.get(key))
            if b is None:
                self.state.others.pop(key, None)
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
            b = _alive(st.others.get(key))
            if b is None:
                made = make_base(Kind.BASE, parent, name, self.config.uid, checked=st.checked, strict=True)
                assert made is not None
                b = made
                st.others[key] = b
            if cross_uid and not b.cross_uid:
                raise _base_error(BaseError.REFUSED, path)
            return b

    def real_len_guess(self, *, cross_uid: bool, given: bytes | None) -> int | None:
        """The length of the real path of the B a spelling would go through
        (SPEC-routes: B is not made, and no other place tried, for a spelling
        known not to fit): the B held already, ``Options.base``'s, or else
        that of the first place it would be made in (its real path there,
        its ancestors safe; for ``cross_uid`` searchable by others).
        ``None`` when no place could hold B."""
        if given is not None:
            parent, name = _path.split(given)
            try:
                return len(_path_join(os.fsencode(os.path.realpath(parent, strict=True)), name))
            except OSError:
                return None
        held = self.state.cross if cross_uid else self.state.auto
        if held is not None and not held.closed:
            return len(held.real)
        for place in self.config.places:
            key = _place_key(place)
            if key is None:
                continue
            made = self.state.places.get(key)
            if made is not None and not made.closed:
                if made.cross_uid or not cross_uid:
                    return len(made.real)
                continue
            real_parent = key[0]
            if not safe_ancestors(real_parent, self.config.uid):
                continue
            if cross_uid and not searchable_by_others(real_parent):
                continue
            return len(_path_join(real_parent, place.name))
        return None


def _alive(b: Base | None) -> Base | None:
    """``b`` while it is held and still there; a B removed since it was made
    is let go (its fds closed), to be made again (SPEC-routes)."""
    if b is None or b.closed:
        return None
    if b.gone():
        b.close()
        return None
    return b


def _place_key(place: Place) -> tuple[bytes, bytes] | None:
    """A place by its real path and B's name there (``None`` when it does
    not resolve)."""
    try:
        return os.fsencode(os.path.realpath(place.path, strict=True)), place.name
    except OSError:
        return None


def check_given(path: bytes, uid: int, *, cross_uid: bool, held: Base | None = None) -> None:
    """Check ``Options.base`` before any route (SPEC-routes "B の作り方"),
    making nothing, on every call: an absolute path ending with a name
    (``ValueError`` otherwise), its parent there (``ENOENT``) and a
    directory (``ENOTDIR``), every directory from the root to it safe
    (``EPERM``), and B itself, when it is there, a real directory of ours
    nobody else can write to (``ENOTDIR`` / ``EPERM``). With ``cross_uid``,
    every directory on the way and B itself must let others search them
    (``EPERM``): judged when B was first held (``held``) and not again."""
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
    if not safe_ancestors(real_parent, uid):
        raise _base_error(BaseError.REFUSED, path)
    if cross_uid and held is not None:
        if not held.cross_uid:
            raise _base_error(BaseError.REFUSED, path)
        cross_uid = False
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
    made where it is not). A place or a B that fails its check is passed over
    without an error. Nothing else in B is touched (SPEC "sweep")."""
    for place in env.config.places:
        key = _place_key(place)
        # A place that fails the check on the way is passed over in silence,
        # as a B that fails its own (in _sweep_at).
        if key is None or not safe_ancestors(key[0], env.config.uid):
            continue
        _sweep_at(place.kind, place.path, place.name, env.config.uid)


def sweep_given(env: Env, path: bytes) -> None:
    """:func:`sweep` for the B given as ``Options.base``, checked as
    :func:`check_given` checks it (nothing is made): a missing parent or B is
    nothing to sweep (success); the errors of the check otherwise."""
    try:
        check_given(path, env.config.uid, cross_uid=False)
    except OSError as e:
        if e.errno == BaseError.NO_DIRECTORY.value:
            return
        raise
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


_D_NAME = re.compile(f"[{_ALPHABET}]{{{RANDOM_LEN}}}")
"""The form of D's name: :data:`RANDOM_LEN` letters and digits. Only
directories of this name in B are swept (B is sockat's alone)."""


def _sweep_base(bfd: int, uid: int) -> None:
    """Remove each D in B (``bfd``) whose lock can be made exclusive, with
    the E in it. A D holding anything but symlinks is left; the E right in B
    (the default connect's) are not touched."""
    with os.scandir(bfd) as entries:
        names = [os.fsencode(e.name) for e in entries if _D_NAME.fullmatch(e.name) and e.is_dir(follow_symlinks=False)]
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
