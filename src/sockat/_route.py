"""The order in which the ways of reaching a long socket path are tried
(docs/decisions/DR-0001, and the language-neutral SPEC of the hub
kawaz/sockat). :func:`run` is the only entry point used by the public API; the
per-route functions are module-level so the tests can force each route."""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import socket
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeAlias

from . import _path
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
    PROC_FD = "proc-fd"
    """``/proc/self/fd/<dirfd>/<name>``, through the ``socket`` module (Linux)."""
    RELATIVE = "relative"
    """The path spelled relative to the cwd, through the ``socket`` module."""
    WIDE = "wide"
    """The path itself in a widened ``sockaddr_un`` (Apple)."""
    WIDE_RELATIVE = "wide-relative"
    """The relative spelling in a widened ``sockaddr_un`` (Apple)."""
    SYMLINK_TO_DIR = "symlink-to-dir"
    """Through a short temporary symlink to the socket's directory (bind)."""
    SYMLINK_TO_DIR_WIDE = "symlink-to-dir-wide"
    """:attr:`SYMLINK_TO_DIR` in a widened ``sockaddr_un`` (Apple)."""
    SYMLINK_TO_FILE = "symlink-to-file"
    """Through a short temporary symlink to the socket file itself (connect)."""
    SYMLINK_TO_FILE_WIDE = "symlink-to-file-wide"
    """:attr:`SYMLINK_TO_FILE` in a widened ``sockaddr_un`` (Apple)."""
    AT = "at"
    """``bindat(2)`` / ``connectat(2)`` on the directory fd (FreeBSD)."""
    THREAD_CWD = "thread-cwd"
    """The file name, from a throwaway thread whose own cwd is the directory
    (Linux, Apple)."""


def _enametoolong() -> OSError:
    return OSError(errno.ENAMETOOLONG, os.strerror(errno.ENAMETOOLONG))


def _join(a: bytes, b: bytes) -> bytes:
    return a + b if a.endswith(b"/") else a + b"/" + b


@dataclass(frozen=True)
class Config:
    """What the route order reads from the environment, made explicit so the
    tests can vary it without touching the process environment."""

    tmp_bases: tuple[bytes, ...]
    """Where the temporary symlink may go, in the order tried: ``TMPDIR``
    when it is set to an absolute path, then ``/tmp``."""
    wide: bool = IS_APPLE
    """Whether to try the widened ``sockaddr_un`` at all (Apple). The tests
    turn it off to stand in for a kernel that refuses the widened form."""
    proc_self_fd: bytes = b"/proc/self/fd"
    """Where the kernel lists the fds of the process (Linux). The tests point
    it elsewhere to stand in for a system without /proc."""
    thread_cwd: bool = HAS_THREAD_CWD
    """Whether to try the thread-cwd route at all (Linux, Apple). The tests
    turn it off to stand in for a system where it cannot be used."""

    @classmethod
    def from_env(cls) -> Config:
        return cls.from_tmpdir(os.environb.get(b"TMPDIR"))

    @classmethod
    def from_tmpdir(cls, tmpdir: bytes | None) -> Config:
        """The configuration for a ``TMPDIR`` of ``tmpdir``: ``TMPDIR``, then
        ``/tmp``. An unset or empty ``TMPDIR`` leaves ``/tmp`` alone; a
        relative one is left out (the link's place would depend on the cwd);
        one that is ``/tmp`` itself is tried once."""
        bases: list[bytes] = []
        if tmpdir and tmpdir.startswith(b"/") and _path.components(tmpdir) != [b"tmp"]:
            bases.append(tmpdir)
        bases.append(b"/tmp")
        return cls(tmp_bases=tuple(bases))


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
        return plain_op(req, spelled), Route.DIRECT
    check_no_nul(raw)
    if len(raw) <= DIRECT_MAX:
        return plain_op(req, spelled), Route.DIRECT
    return run_long(req, raw, Config.from_env())


def check_no_nul(path: bytes) -> None:
    """Raise :class:`ValueError` when ``path`` contains a NUL byte."""
    if b"\0" in path:
        raise ValueError("socket path must not contain a NUL byte")


def run_long(req: Request, path: bytes, cfg: Config) -> tuple[socket.socket, Route]:
    """The routes for a path longer than :data:`DIRECT_MAX`, with what they
    read from the environment given as ``cfg`` (the tests pass their own).

    FreeBSD: ``bindat(2)`` / ``connectat(2)`` and nothing else. Linux:
    ``/proc/self/fd/<dirfd>/<name>`` with the directory opened ``O_PATH``; when
    that cannot be used, the POSIX order (:func:`posix_run`). Every other
    system: the POSIX order.
    """
    check_no_nul(path)
    directory, name = _path.split(path)
    if IS_FREEBSD:
        return at_op(req, directory, name), Route.AT
    if IS_LINUX:
        dirfd = open_dir(directory)
        try:
            via = proc_spelling(dirfd, name, cfg.proc_self_fd)
            if via is not None:
                return plain_op(req, via), Route.PROC_FD
            return posix_run(req, path, directory, name, dirfd, cfg)
        finally:
            os.close(dirfd)
    return posix_run(req, path, directory, name, None, cfg)


def new_socket(req: Request) -> socket.socket:
    """A fresh ``AF_UNIX`` / ``SOCK_STREAM`` socket made by the ``socket``
    module (so set up exactly as its own), nonblocking when ``req`` asks."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
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


def plain_op(req: Request, spelling: str | bytes) -> socket.socket:
    """Bind (and listen) or connect with ``spelling`` through the ``socket``
    module, as a caller of the standard library would."""
    sock = new_socket(req)
    if req.op is Op.BIND:
        return _finish(req, sock, lambda s: s.bind(spelling))
    return _finish(req, sock, lambda s: s.connect(spelling))


def raw_op(req: Request, addr: bytes) -> socket.socket:
    """Bind (and listen) or connect a fresh socket to the address bytes
    ``addr`` through the C library (an address the ``socket`` module would
    refuse)."""
    sock = new_socket(req)
    return _finish(req, sock, lambda s: op(req.op, s, addr))


def wide_op(req: Request, spelling: bytes) -> socket.socket:
    """Bind (and listen) or connect a fresh socket to ``spelling`` in a
    widened ``sockaddr_un`` (Apple)."""
    return raw_op(req, sockaddr(spelling, wide=True))


def refuses_wide(e: OSError) -> bool:
    """Whether an error from a widened-address call means the kernel does not
    take the widened form (as opposed to a real outcome such as ``ENOENT``,
    ``EADDRINUSE`` or ``ECONNREFUSED``). XNU reports an address it will not
    take with ``EINVAL``, or ``ENAMETOOLONG`` for one longer than
    ``SOCK_MAXADDRLEN``."""
    return e.errno in (errno.EINVAL, errno.ENAMETOOLONG)


def open_dir(directory: bytes) -> int:
    """Open ``directory`` to use as the base of a relative bind / connect
    (close-on-exec, as :func:`os.open` makes every fd).

    Linux: ``O_PATH | O_DIRECTORY``, which needs only search permission. The
    fd serves the /proc route (``/proc/self/fd/<fd>`` names the directory).

    Apple: ``O_SEARCH``, which needs only search permission; a kernel that does
    not take ``O_SEARCH`` on a directory answers ``EISDIR`` or ``EINVAL``, and
    then the directory is opened ``O_RDONLY`` (:func:`search_or_read_only`).

    Other systems: ``O_RDONLY | O_DIRECTORY`` (needs read permission).
    """
    if sys.platform == "linux" or sys.platform == "android":
        return os.open(directory, os.O_PATH | os.O_DIRECTORY)
    if sys.platform == "darwin" or sys.platform == "ios":
        # os.O_SEARCH is there on Apple systems for every supported Python;
        # typeshed declares it only from 3.13 on, hence the lookup by name.
        o_search: int | None = getattr(os, "O_SEARCH", None)
        if o_search is not None:
            flags = o_search
            return search_or_read_only(directory, lambda: os.open(directory, flags))
    return _open_read_only(directory)


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


def proc_spelling(dirfd: int, name: bytes, proc_self_fd: bytes) -> bytes | None:
    """``<proc_self_fd>/<dirfd>/<name>``, or ``None`` when the /proc route
    cannot be used (Linux):

    * ``<proc_self_fd>/<dirfd>`` cannot be looked up (no /proc). Without this
      check a bind / connect through the spelling would fail with ``ENOENT``,
      which reads as "the directory does not exist" although it does;
    * the spelling does not fit ``sun_path`` (a file name of about 90 bytes or
      more).
    """
    fd_dir = _join(proc_self_fd, str(dirfd).encode())
    try:
        os.stat(fd_dir)
    except OSError:
        return None
    via = _join(fd_dir, name)
    return via if len(via) <= DIRECT_MAX else None


def at_op(req: Request, directory: bytes, name: bytes) -> socket.socket:
    """FreeBSD, for a path longer than :data:`DIRECT_MAX`: ``bindat(2)`` /
    ``connectat(2)`` with the file name relative to the directory fd.

    The directory is opened first, so a missing directory is ``ENOENT`` before
    the length of the file name is judged."""
    dirfd = open_dir(directory)
    try:
        addr = sockaddr(name)
        sock = new_socket(req)
        return _finish(req, sock, lambda s: op_at(req.op, s, dirfd, addr))
    finally:
        os.close(dirfd)


# ---------- the POSIX order ----------

SPELLING_MAX = WIDE_PATH_MAX if IS_APPLE else DIRECT_MAX
"""The longest spelling a sockaddr carries on this system: the widened
form's 253 bytes on Apple, :data:`DIRECT_MAX` elsewhere."""


def posix_run(
    req: Request,
    path: bytes,
    directory: bytes,
    name: bytes,
    opened: int | None,
    cfg: Config,
) -> tuple[socket.socket, Route]:
    """Try, in order, for a path longer than :data:`DIRECT_MAX` whose
    directory part is ``directory`` and file name ``name``:

    1. the path spelled relative to the cwd, when that is at most
       :data:`DIRECT_MAX` bytes;
    2. (Apple) a widened ``sockaddr_un`` with the path itself, or failing that
       with the relative spelling, when one of them is at most 253 bytes;
    3. a short symlink (:func:`symlink_route`: to the socket's directory for a
       bind, to the socket file for a connect) under the first of
       :attr:`Config.tmp_bases` where the path through it fits and the symlink
       can be created;
    4. (Linux, Apple) the file name from a throwaway thread whose own cwd is
       the directory (:func:`thread_cwd_op`);
    5. the place of the fork route, which this implementation does not have
       (DR-0001): ``ENAMETOOLONG``.

    Before 4 the directory is opened (``opened`` when the caller has it open
    already, as Linux does), so a missing or unreadable directory is the real
    error it is, before any length is judged.

    A failure of a route ends the search with that error, except where it
    shows that the route itself cannot be used (the kernel refusing the
    widened form, the symlink not being creatable); then the next route (or
    the next place for the symlink) is tried.
    """
    rel = Relative.new(path, directory)

    if rel is not None and len(rel.path) <= DIRECT_MAX:
        return rel.run(lambda p: plain_op(req, p)), Route.RELATIVE

    wide = IS_APPLE and cfg.wide
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

    for base in cfg.tmp_bases:
        limit = WIDE_PATH_MAX if wide else DIRECT_MAX
        outcome = symlink_route(req, base, path, directory, name, limit)
        if isinstance(outcome, tuple):
            return outcome
        if outcome is Skip.WIDE_REFUSED:
            wide = False

    dirfd = opened if opened is not None else open_dir(directory)
    try:
        if cfg.thread_cwd:
            done = thread_cwd_op(req, dirfd, name, wide)
            if isinstance(done, socket.socket):
                return done, Route.THREAD_CWD
    finally:
        if opened is None:
            os.close(dirfd)
    # The place of the fork route (bind; the SPEC's connect_any family), which
    # this implementation does not have (DR-0001): what a plain bind / connect
    # of a path longer than sun_path answers.
    raise _enametoolong()


def thread_cwd_op(req: Request, dirfd: int, name: bytes, wide: bool) -> socket.socket | Skip:
    """Bind (and listen) or connect a fresh socket to ``name`` from a
    throwaway :class:`threading.Thread` whose own cwd is the directory
    ``dirfd`` (:func:`._sys.enter_dir_in_this_thread`: ``unshare(CLONE_FS)``
    then ``fchdir(2)`` on Linux, ``pthread_fchdir_np`` on Apple). The process
    cwd, and so every other thread's, never changes; the thread ends with the
    call.

    :attr:`Skip.UNUSABLE` when the route cannot be used: the file name does
    not fit a sockaddr (the widened one on Apple when ``wide``), the thread
    cannot be started, or the system refuses a cwd of the thread's own
    (a seccomp filter on, as under docker's default, or ``unshare`` refused; no
    ``pthread_fchdir_np``, or it failed), or the kernel refuses the widened
    sockaddr a file name over :data:`DIRECT_MAX` bytes needs (Apple:
    :func:`refuses_wide`). Any other failure is the outcome.
    """
    limit = WIDE_PATH_MAX if wide else DIRECT_MAX
    if len(name) > limit:
        return Skip.UNUSABLE
    addr = sockaddr(name, wide=True) if len(name) > DIRECT_MAX else None
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
            if addr is not None:
                op(req.op, sock, addr)
            elif req.op is Op.BIND:
                sock.bind(name)
            else:
                sock.connect(name)
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
    if result is Skip.UNUSABLE or (isinstance(result, OSError) and addr is not None and refuses_wide(result)):
        sock.close()
        return Skip.UNUSABLE
    if isinstance(result, BaseException):
        sock.close()
        raise result
    return _finish(req, sock, lambda s: None)


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
        :class:`CwdChanged` is raised. A socket file that such a bind created
        is left where it is, because the directory it went to is unknown. The
        check cannot see a change that is undone before it runs; the guarantee
        is for processes that do not change the cwd while binding /
        connecting.
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
                "the current directory changed while binding / connecting through a path "
                "relative to it; the result was discarded"
            ) from failure
        if failure is not None:
            raise failure
        assert result is not None
        return result


# ---------- the symlink route ----------


class Skip(Enum):
    """Why :func:`symlink_route` did not run."""

    UNUSABLE = "unusable"
    """The route could not be used (too long, no usable base, symlink not
    creatable)."""
    WIDE_REFUSED = "wide-refused"
    """The path through the symlink needed the widened form and the kernel
    refused it (Apple)."""


LINK_PREFIX = b"sockat."
"""What the symlink name starts with, so a link left behind by a process that
died is recognisable as sockat's."""

LINK_RANDOM_LEN = 10
"""Length of the random part of the symlink name (``sockat.XXXXXXXXXX``)."""

_ALPHABET = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def random_name() -> bytes:
    """``sockat.`` followed by :data:`LINK_RANDOM_LEN` random letters and
    digits from the system's secure generator (:mod:`secrets`; when it fails,
    the symlink route cannot be used)."""
    return LINK_PREFIX + bytes(secrets.choice(_ALPHABET) for _ in range(LINK_RANDOM_LEN))


def symlink_route(
    req: Request,
    base: bytes,
    path: bytes,
    directory: bytes,
    name: bytes,
    limit: int,
) -> tuple[socket.socket, Route] | Skip:
    """Bind / connect ``path`` (directory part ``directory``, file name
    ``name``) through a symlink ``<base>/sockat.XXXXXXXXXX``:

    ======= ================== ============================== =========================
    request the link points to spelling handed to the kernel route
    ======= ================== ============================== =========================
    bind    ``directory``      ``<link>/<name>``              :attr:`Route.SYMLINK_TO_DIR`
    connect ``path``           ``<link>``                     :attr:`Route.SYMLINK_TO_FILE`
    ======= ================== ============================== =========================

    A connect follows a symlink in the last component, so the link to the
    file reaches the socket by a spelling whose length depends on neither the
    directory nor the file name. A bind does not: Linux refuses it with
    ``EADDRINUSE``. Both links hold the caller's own spelling (made absolute
    against the cwd), so the kernel resolves them as it resolves ``path``.

    Used when the spelling is at most ``limit`` bytes (:data:`DIRECT_MAX` for
    the plain sockaddr, or on Apple the widened form's 253) and ``base`` is a
    place where nobody else can replace the symlink (:func:`safe_base`). Both
    are checked before anything is created. Nothing is left under ``base``:
    the symlink is removed when this returns, whatever the outcome.
    """
    to_file = req.op is Op.CONNECT
    placeholder = _join(base, LINK_PREFIX + b"X" * LINK_RANDOM_LEN)
    spelling_len = len(placeholder) if to_file else len(_join(placeholder, name))
    if spelling_len > limit or not safe_base(base):
        return Skip.UNUSABLE
    target = path if to_file else directory
    if not target.startswith(b"/"):
        try:
            target = _join(os.getcwdb(), target)
        except OSError:
            return Skip.UNUSABLE
    try:
        link = TempLink.create(base, target)
    except (OSError, NotImplementedError):
        return Skip.UNUSABLE
    with link:
        if to_file:
            via, plain_route, wide_route = link.path, Route.SYMLINK_TO_FILE, Route.SYMLINK_TO_FILE_WIDE
        else:
            via, plain_route, wide_route = _join(link.path, name), Route.SYMLINK_TO_DIR, Route.SYMLINK_TO_DIR_WIDE
        if IS_APPLE and len(via) > DIRECT_MAX:
            try:
                return wide_op(req, via), wide_route
            except OSError as e:
                if refuses_wide(e):
                    return Skip.WIDE_REFUSED
                raise
        return plain_op(req, via), plain_route


def safe_base(base: bytes) -> bool:
    """Whether ``base``, followed through symlinks (macOS's ``/tmp`` is one to
    ``/private/tmp``), is a directory where no other user (root aside) can
    remove or rename an entry, so the symlink cannot be replaced between its
    creation and the bind / connect through it (:func:`only_we_can_replace`).
    ``False`` when it cannot be looked up."""
    try:
        st = os.stat(base)
    except OSError:
        return False
    return only_we_can_replace(st.st_mode, st.st_uid, os.geteuid())


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

    ATTEMPTS = 16
    """How many names to try before giving up. Each try is a fresh random name
    from 62^10 possibilities, so exhausting this means something other than
    bad luck (for example a base that refuses every name)."""

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
        skipped for the next one. Any other error ends the attempt.
        """
        for _ in range(cls.ATTEMPTS):
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
        # Best effort: the socket is already bound / connected, and failing it
        # now would not bring the symlink back either.
        with contextlib.suppress(OSError):
            os.unlink(self.path)
