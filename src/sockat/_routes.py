"""The routes to a long socket path and the order they are tried in (the hub
SPEC's "長いパスの経路と試す順番" and SPEC-routes). The public calls go
through :func:`bind` / :func:`connect` / :func:`resolve_for_bind` /
:func:`resolve_for_connect`; each route is a module-level function so the
tests can force it."""

from __future__ import annotations

import contextlib
import errno
import os
import socket
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeAlias

from . import _path
from ._errors import CwdChanged
from ._place import RANDOM_LEN, Base, Dir, Env, Fits, Kind, LinkFile, open_dir
from ._socket_file import At, Link, Recorder
from ._sockets import PathSocket
from ._spelling import CwdRelative, Dangling, Linked, Original, Spelling, Trail, Until
from ._sys import (
    DIRECT_MAX,
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

STANDARD = "standard"
LINK_DIR = "link-dir"
LINK_FILE = "link-file"
THREAD_CWD = "thread-cwd"
AT = "at"
PROC_DIRECT = "proc-direct"
RELATIVE = "relative"
WIDENED = "widened"
AT_DIRECT = "at-direct"
THREAD_CWD_DIRECT = "thread-cwd-direct"

HAS_THREAD_CWD = IS_LINUX or IS_APPLE
"""Whether this system has a means of giving a thread a cwd of its own
(thread-cwd, thread-cwd-direct)."""

TOO_LONG = "spelling too long"
PROC_UNUSABLE = "/proc unusable"
NO_PLACE_FOR_B = "no place for B"
NO_CROSS_UID_B = "no B other uids can follow"
NO_D = "D not made"
NO_E = "E not made"
THREAD_CWD_REFUSED = "thread cwd refused"
SOCKET_DIR_NOT_OPENED = "socket directory not opened"
NOT_FOLLOWED = "symlink not followed by the bind"


def enametoolong() -> OSError:
    return OSError(errno.ENAMETOOLONG, os.strerror(errno.ENAMETOOLONG))


def _join(a: bytes, b: bytes) -> bytes:
    return a + b if a.endswith(b"/") else a + b"/" + b


# ---------- what a call is ----------


@dataclass
class Call:
    """One bind / connect: what to do and on which socket."""

    op: Op
    type: int = socket.SOCK_STREAM
    nonblocking: bool = False
    """Make the socket nonblocking before the connect, and leave it so."""
    sock: socket.socket | None = None
    """The caller's socket (a datagram connect); ``None``: a fresh one per
    route."""
    recorder: Recorder | None = None
    """For ``sockat.ex``'s binds: remember the socket file right after the
    bind."""

    @property
    def listens(self) -> bool:
        return self.op is Op.BIND and self.type == socket.SOCK_STREAM

    def new_socket(self, *, standard: bool = False) -> socket.socket:
        """The socket a route works on: the caller's, or a fresh
        ``AF_UNIX`` socket made by the ``socket`` module (so set up as its
        own), a :class:`PathSocket` for a long path (``standard``: a plain
        :class:`socket.socket`)."""
        if self.sock is not None:
            return self.sock
        cls = socket.socket if standard else PathSocket
        sock = cls(socket.AF_UNIX, self.type)
        if self.nonblocking:
            sock.setblocking(False)
        return sock

    def finish(
        self, sock: socket.socket, do: Callable[[socket.socket], None], *, where: At | None = None
    ) -> socket.socket:
        """Run ``do`` on ``sock``, then after a bind remember the file
        ``where`` says (:meth:`Recorder.bound`) and listen (stream); on any
        failure close ``sock`` when it is not the caller's."""
        try:
            do(sock)
            if self.op is Op.BIND:
                if self.recorder is not None:
                    assert where is not None
                    self.recorder.bound(where)
                if self.listens:
                    sock.listen()
        except BaseException:
            if sock is not self.sock:
                sock.close()
            raise
        return sock

    def plain(self, spelling: str | bytes, *, standard: bool = False, where: At | None = None) -> socket.socket:
        """Bind / connect with ``spelling`` through the ``socket`` module."""
        sock = self.new_socket(standard=standard)
        if self.op is Op.BIND:
            return self.finish(sock, lambda s: s.bind(spelling), where=where)
        return self.finish(sock, lambda s: s.connect(spelling))

    def wide(self, spelling: bytes) -> socket.socket:
        """Connect with ``spelling`` in a widened ``sockaddr_un`` (Apple)."""
        addr = sockaddr(spelling, wide=True)
        return self.finish(self.new_socket(), lambda s: op(self.op, s, addr))

    def at(self, dirfd: int, spelling: bytes, *, where: At | None = None) -> socket.socket:
        """``bindat(2)`` / ``connectat(2)`` of ``spelling`` on ``dirfd``
        (FreeBSD)."""
        addr = sockaddr(spelling)
        return self.finish(self.new_socket(), lambda s: op_at(self.op, s, dirfd, addr), where=where)


@dataclass(frozen=True)
class Target:
    """A long path, looked up: its directory (found to be one) and its
    realpath, and its file name."""

    path: bytes
    directory: bytes
    name: bytes
    real_dir: bytes


def check_no_nul(path: bytes) -> None:
    if b"\0" in path:
        raise ValueError("socket path must not contain a NUL byte")


class RealPathTooLong(Exception):
    """The relative path's directory is there but its realpath cannot be
    looked up for its length (a cwd past PATH_MAX: ``ENAMETOOLONG``, or
    ``ERANGE`` where the cwd cannot be read), or the realpath had is too
    long to be looked up itself: for a bind or a resolve, the routes cannot
    be used (SPEC "相対パスを絶対パスにする")."""


def look_up(path: bytes, *, absolutizing: bool = False) -> Target:
    """The SPEC's steps before any route: a path that does not end with a
    file name is an argument error; the socket's directory is looked up
    (``ENOENT``, ``ENOTDIR``) and resolved to its realpath (its error), all
    before any length is judged or anything is made. ``absolutizing`` (a
    bind or a resolve): a relative path whose realpath cannot be looked up
    for its length raises :class:`RealPathTooLong` instead."""
    check_no_nul(path)
    directory, name = _path.split(path)
    st = os.stat(directory)
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), os.fsdecode(directory))
    try:
        real_dir = os.fsencode(os.path.realpath(directory, strict=True))
        if absolutizing and not path.startswith(b"/"):
            # os.path.realpath resolves a cwd past PATH_MAX (as glibc's
            # realpath(3) does) to a path no call takes: looked up, it is
            # ENAMETOOLONG, and is taken as not had (SPEC).
            os.stat(real_dir)
    except OSError as e:
        if absolutizing and e.errno in (errno.ENAMETOOLONG, errno.ERANGE) and not path.startswith(b"/"):
            raise RealPathTooLong() from e
        raise
    return Target(path, directory, name, real_dir)


def absolute(path: bytes) -> bytes | None:
    """``path`` with the cwd put in front when it is relative (as the kernel
    resolves it), or ``None`` when the cwd cannot be read."""
    if path.startswith(b"/"):
        return path
    try:
        return _join(os.getcwdb(), path)
    except OSError:
        return None


# ---------- /proc ----------


def proc_usable(env: Env, *, pid: bool) -> bool:
    """Whether /proc lists this process's fds as what they are, looked up
    for an fd of ``/`` opened for the purpose (SPEC "用語": "/proc を使える"):
    ``/proc/self/fd/<fd>`` (``pid`` false: choosing the connect's row) or
    ``/proc/<pid>/fd/<fd>`` (``pid``: procB's estimate before B is opened)
    gives the ``(st_dev, st_ino)`` of the fd (a /proc of another PID
    namespace or mount does not)."""
    proc = env.config.proc
    if proc is None:
        return False
    try:
        probe = open_dir(b"/")
    except OSError:
        return False
    try:
        who = b"%d" % os.getpid() if pid else b"self"
        try:
            via = os.stat(b"%s/%s/fd/%d" % (proc, who, probe))
            held = os.fstat(probe)
        except OSError:
            return False
        return (via.st_dev, via.st_ino) == (held.st_dev, held.st_ino)
    finally:
        os.close(probe)


# ---------- B for a call ----------


@dataclass
class Bases:
    """The B a call may use, looked up when first wanted."""

    env: Env
    target: Target
    given: bytes | None
    cross_uid: bool
    _plain: Base | None = field(default=None, init=False)
    _plain_tried: bool = field(default=False, init=False)
    _cross: Base | None = field(default=None, init=False)
    _cross_tried: bool = field(default=False, init=False)
    share_e: bool = False
    """The default bind (no SocketFile): link-dir's E is the one kept per
    socket directory, used again (SPEC-routes "link-dir")."""
    occupied: list[bytes] = field(default_factory=list, init=False)
    """The B at a place found taken over by someone else in this call (the
    place passed over)."""
    _no_d: list[Base] = field(default_factory=list, init=False)
    _too_long: list[bool] = field(default_factory=list, init=False)
    """The B whose D could not be made in this call (SPEC-routes: not tried
    again on the same B within the call; a later call tries again)."""

    def no_b(self, why: str = "") -> str:
        """Why no B could be had: one found taken over by someone else is
        said so (SPEC: the reason tells an occupied B)."""
        if self.occupied:
            return "B occupied by another owner: " + ", ".join(os.fsdecode(p) for p in self.occupied)
        return why or NO_PLACE_FOR_B

    def too_long(self) -> str:
        """The spelling does not fit; a B found taken over on the way in this
        call is said too (SPEC: the reason tells an occupied B)."""
        if self.occupied:
            return TOO_LONG + "; " + self.no_b()
        return TOO_LONG

    def dir(self, b: Base) -> Dir | None:
        """D of ``b`` for this call, or ``None`` when it cannot be made (then
        remembered for the rest of the call)."""
        if b in self._no_d:
            return None
        d = b.dir(self.env.config.uid)
        if d is None:
            self._no_d.append(b)
        return d

    def plain(self, fits: Fits | None = None) -> Base | None:
        """The process's B (``Options.base`` when given, raising what fails),
        not the one in the socket's directory. ``fits``: a spelling through
        its real path must fit (no B is made, nor a later place tried, for
        one that would not: :meth:`too_long_seen`)."""
        if self._plain is not None:
            return self._plain
        if self.given is not None:
            if not self._plain_tried:
                self._plain_tried = True
                self._plain = self.env.given_base(self.given, cross_uid=self.cross_uid)
            return self._plain
        self._plain_tried = True
        self._plain = self.env.auto_base(self.occupied, fits, self._too_long)
        return self._plain

    def made_plain(self) -> Base | None:
        """The process's B (or ``Options.base``'s) when it is made already;
        nothing is made."""
        if self._plain_tried:
            return self._plain
        if self.given is not None:
            return None
        auto = self.env.state.auto
        return auto if auto is not None and not auto.closed else None

    def cross(self, fits: Fits | None = None) -> Base | None:
        """B another uid can follow a spelling through (``Options.base`` when
        given: checked so)."""
        if self._cross is not None:
            return self._cross
        if self.given is not None:
            if not self._cross_tried:
                self._cross_tried = True
                self._cross = self.env.given_base(self.given, cross_uid=True)
            return self._cross
        self._cross_tried = True
        self._cross = self.env.cross_base(self.occupied, fits, self._too_long)
        return self._cross

    def for_real_path(self, fits: Fits | None = None) -> Base | None:
        """B for a spelling by B's real path."""
        return self.cross(fits) if self.cross_uid else self.plain(fits)

    def too_long_seen(self) -> bool:
        """Whether the last lookup stopped at a place the spelling would not
        fit through."""
        seen = bool(self._too_long)
        self._too_long.clear()
        return seen

    def with_sockdir(self) -> Base | None:
        """The process's B, or else one in the socket's directory (where only
        procB and thread-cwd / at can use it)."""
        b = self.plain()
        if b is None and self.given is None:
            b = self.env.sockdir_base(self.target.real_dir)
        return b


def _fd_digits_guess() -> int:
    """How many digits the next fd is likely to take (for judging a procB
    spelling's length before B is made)."""
    try:
        fd = os.open(b"/", os.O_RDONLY)
    except OSError:
        return 2
    os.close(fd)
    return len(str(fd + 1))


# ---------- spellings through B ----------


@dataclass
class Through:
    """A spelling made through B, D and E, with what it is good for."""

    spelling: bytes
    route: str
    base: str
    link: Link
    linked: Linked
    target: bytes
    """What E points to: the realpath of the socket's directory (link-dir)
    or the socket file (link-file)."""


class _Unusable(Exception):
    """A route cannot be used (``why``); internal to the routes."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def _proc_len(env: Env, b: Base | None, fd_digits: int) -> int:
    proc = env.config.proc or b""
    fd_part = len(str(b.fd)) if b is not None else fd_digits
    return len(b"%s/%d/fd/" % (proc, os.getpid())) + fd_part


def _proc_base_name(b: Base) -> str:
    """The diagnostic ``base`` of procB: ``proc`` for B in a temporary place
    (the same spelling whichever it is), the B itself for one that is not
    (``sockdir``, ``given``)."""
    return b.kind.value if b.kind in (Kind.SOCKDIR, Kind.BASE) else "proc"


def _through_procb(env: Env, t: Target, bases: Bases, route: str, target: bytes, name: bytes | None) -> Through:
    """procB: ``/proc/<pid>/fd/<B's fd>/D/E[/<name>]``, E a symlink in D to
    ``target``. Not for ``usable_cross_uid`` (another uid cannot follow it);
    only where /proc names B (:meth:`Base.proc_usable`). The length is judged
    before B is made (E's name at least one character)."""
    if bases.cross_uid:
        raise _Unusable("procB not followed by other uids")
    if not proc_usable(env, pid=True):
        raise _Unusable(PROC_UNUSABLE)
    tail = 1 + RANDOM_LEN + 1 + 1 + (1 + len(name) if name is not None else 0)
    made = bases.made_plain()
    if _proc_len(env, made, _fd_digits_guess()) + tail > DIRECT_MAX:
        raise _Unusable(TOO_LONG)
    b = bases.with_sockdir()
    if b is None:
        raise _Unusable(bases.no_b())
    proc = env.config.proc
    assert proc is not None
    pid = os.getpid()
    if not b.proc_usable(proc, pid):
        raise _Unusable(PROC_UNUSABLE)
    head = b.proc(proc, pid)
    if len(head) + tail > DIRECT_MAX:
        raise _Unusable(bases.too_long())
    spelling, link = _link_in_d(bases, b, head, target, name, share=bases.share_e and route == LINK_DIR)
    return Through(
        spelling,
        route,
        _proc_base_name(b),
        link,
        Linked(removable=name is not None, until=Until.PROCESS, usable_cross_uid=False, usable_with_private_tmp=True),
        target,
    )


def _through_real(env: Env, t: Target, bases: Bases, route: str, target: bytes, name: bytes | None) -> Through:
    """B's real path: ``<B>/D/E[/<name>]``. B is not made when the spelling
    cannot fit through any place (judged from the places' real paths)."""
    tail = 1 + RANDOM_LEN + 1 + 1 + (1 + len(name) if name is not None else 0)
    if bases.given is not None:
        given_len = env.given_len(bases.given)
        if given_len is None:
            raise _Unusable(bases.no_b())
        if given_len + tail > DIRECT_MAX:
            raise _Unusable(TOO_LONG)
    b = bases.for_real_path(lambda n: n + tail <= DIRECT_MAX)
    if b is None:
        if bases.too_long_seen():
            raise _Unusable(bases.too_long())
        raise _Unusable(bases.no_b(NO_CROSS_UID_B if bases.cross_uid else NO_PLACE_FOR_B))
    if len(b.real) + tail > DIRECT_MAX:
        raise _Unusable(bases.too_long())
    spelling, link = _link_in_d(bases, b, b.real, target, name, share=bases.share_e and route == LINK_DIR)
    return Through(
        spelling,
        route,
        b.kind.value,
        link,
        Linked(
            removable=name is not None,
            until=Until.CLEANUP,
            usable_cross_uid=b.cross_uid,
            usable_with_private_tmp=b.private_tmp_ok,
        ),
        target,
    )


def link_dir_proc(env: Env, t: Target, bases: Bases) -> Through:
    """link-dir with procB: ``/proc/<pid>/fd/<B's fd>/D/E/<name>``, E a
    symlink in D to the realpath of the socket's directory."""
    return _through_procb(env, t, bases, LINK_DIR, t.real_dir, t.name)


def link_dir_real(env: Env, t: Target, bases: Bases) -> Through:
    """link-dir with B's real path: ``<B>/D/E/<name>``."""
    return _through_real(env, t, bases, LINK_DIR, t.real_dir, t.name)


def _file_target(t: Target) -> bytes:
    target = absolute(t.path)
    if target is None:
        raise _Unusable("cwd unreadable")
    return target


def link_file_real(env: Env, t: Target, bases: Bases) -> Through:
    """link-file with B's real path: ``<B>/D/E``, E a symlink in D to the
    socket file (the path made absolute against the cwd)."""
    return _through_real(env, t, bases, LINK_FILE, _file_target(t), None)


def link_file_proc(env: Env, t: Target, bases: Bases) -> Through:
    """link-file with procB: ``/proc/<pid>/fd/<B's fd>/D/E`` (only
    ``resolve_for_connect``: a bind does not follow it where /proc is)."""
    return _through_procb(env, t, bases, LINK_FILE, _file_target(t), None)


def _link_in_d(
    bases: Bases, b: Base, head: bytes, target: bytes, name: bytes | None, *, share: bool = False
) -> tuple[bytes, Link]:
    """E in D of ``b`` pointing to ``target``, and the spelling
    ``<head>/D/E[/<name>]`` (checked again to fit, E's name now known).
    ``share`` (the default bind's link-dir): the E kept per target in D, made
    only when there is none that still points there."""
    d = bases.dir(b)
    if d is None:
        raise _Unusable(NO_D)
    # The spelling with the E it would get is judged before E is made, so
    # that no E that would not fit is made (a shared one would stay).
    planned = _join(_join(head, d.name), b"e" * d.next_link_len(target if share else None))
    if name is not None:
        planned = _join(planned, name)
    if len(planned) > DIRECT_MAX:
        raise _Unusable(bases.too_long())
    if share:
        got = d.shared_link(target)
        if got is None:
            raise _Unusable(NO_E)
        e, _ = got
        link = Link(d, e, kept=True)
    else:
        made_e = d.link(target)
        if made_e is None:
            raise _Unusable(NO_E)
        e = made_e
        link = Link(d, e)
    spelling = _join(_join(head, d.name), e)
    if name is not None:
        spelling = _join(spelling, name)
    if len(spelling) > DIRECT_MAX:
        link.discard()
        raise _Unusable(TOO_LONG)
    return spelling, link


# ---------- the throwaway thread ----------


class ThreadRefused(Exception):
    """The system will not give a thread a cwd of its own."""


def in_thread_cwd(dirfd: int, do: Callable[[int], None], owned: tuple[socket.socket, ...] = ()) -> None:
    """Run ``do`` in a throwaway :class:`threading.Thread` whose own cwd is
    the directory ``dirfd`` (SPEC-routes "使い捨てのスレッド"): Linux
    ``unshare(CLONE_FS)`` then ``fchdir``, Apple ``pthread_fchdir_np``. The
    process cwd and the calling thread's never change; the thread ends with
    the call. :class:`ThreadRefused` when the thread cannot be started or
    the system refuses it a cwd of its own; any other failure of ``do`` (or
    of the ``fchdir`` after ``unshare``) is raised.

    The thread works on fds of its own: a ``dup`` of ``dirfd`` (handed to
    ``do`` too) and the sockets in ``owned`` (dups the caller made for it),
    all closed by the thread when it ends. So whatever interrupts the caller
    (``KeyboardInterrupt`` in ``start``, between ``start`` and ``join``, or
    out of ``join``), the caller closing its own fds never lets the thread
    move to, or bind / connect at, a directory whose fd got the same number.
    The caller still waits for the thread to the end before it returns or
    raises."""
    outcome: list[BaseException | None] = []
    refused = ThreadRefused()
    try:
        own = os.dup(dirfd)
    except BaseException:
        for s in owned:
            s.close()
        raise

    def release() -> None:
        with contextlib.suppress(OSError):
            os.close(own)
        for s in owned:
            s.close()

    def run() -> None:
        try:
            if not enter_dir_in_this_thread(own):
                outcome.append(refused)
                return
            do(own)
            outcome.append(None)
        except BaseException as e:
            outcome.append(e)
        finally:
            release()

    thread = threading.Thread(target=run, name="sockat-thread-cwd")
    interrupted: BaseException | None = None
    try:
        thread.start()
    except RuntimeError:
        # The thread could not be started: its fds are the caller's to close.
        release()
        raise ThreadRefused() from None
    except BaseException as e:
        interrupted = e
    while True:
        try:
            thread.join()
            break
        except RuntimeError:
            # Interrupted before it started (join of a thread not started):
            # no thread will close its fds.
            if not thread.is_alive() and thread.ident is None:
                release()
                break
            raise
        except BaseException as e:
            if interrupted is None:
                interrupted = e
    if interrupted is not None:
        raise interrupted
    result = outcome[0] if outcome else RuntimeError("the thread-cwd thread ended without an outcome")
    if result is not None:
        raise result


def thread_cwd_op(call: Call, dirfd: int, spelling: bytes, *, where: At | None = None) -> socket.socket:
    """Bind / connect ``spelling`` from a throwaway thread whose cwd is
    ``dirfd`` (:func:`in_thread_cwd`), through a dup of the socket the
    thread owns; then, in the caller, what follows a bind (remember
    ``where``, listen)."""
    sock = call.new_socket()
    try:
        mine = sock.dup()
        if call.op is Op.BIND:
            in_thread_cwd(dirfd, lambda _: mine.bind(spelling), (mine,))
        else:
            in_thread_cwd(dirfd, lambda _: mine.connect(spelling), (mine,))
    except BaseException:
        if sock is not call.sock:
            sock.close()
        raise
    return call.finish(sock, lambda s: None, where=where)


def thread_cwd_means(env: Env) -> str | None:
    """Why a thread's own cwd cannot be used here (``None``: it may be). Only
    asked on a system that has the means (:data:`HAS_THREAD_CWD`)."""
    if not env.config.thread_cwd:
        return "thread cwd turned off"
    if not thread_cwd_possible():
        return THREAD_CWD_REFUSED
    return None


# ---------- bind ----------


@dataclass
class BindDone:
    """A bind that went through."""

    sock: socket.socket
    route: str
    spelling: Spelling
    base: str | None
    link: Link | None


def bind_standard(call: Call, spelled: str | bytes, raw: bytes, *, plain_type: bool = True) -> BindDone:
    """Give the ``socket`` module ``spelled`` (``raw`` its bytes): an
    absolute path (the one given, or an absolute candidate: ``Original``),
    an abstract name, or a relative path as it is (``CwdRelative``, no route
    could be used). ``plain_type``: a plain :class:`socket.socket` (else a
    :class:`PathSocket`, to report the path given)."""
    relative = not raw.startswith(b"/") and not (IS_LINUX and raw.startswith(b"\0"))
    sock = call.plain(spelled, standard=plain_type, where=At(raw, cwd_relative=relative))
    return BindDone(sock, STANDARD, CwdRelative() if relative else Original(), None, None)


def bind_long(call: Call, t: Target, env: Env, bases: Bases, trail: Trail) -> BindDone | None:
    """The SPEC's bind order past the standard: link-dir (procB, then B's
    real path), link-file (where a bind may follow the symlink),
    thread-cwd / at; ``None`` when none of them can be used (the caller
    raises ``ENAMETOOLONG``, or binds a short relative path as it is)."""
    trail.skip(STANDARD, "path too long")
    for make in (link_dir_proc, link_dir_real):
        try:
            through = make(env, t, bases)
        except _Unusable as u:
            trail.skip(LINK_DIR, u.why)
            continue
        except OSError:
            # Options.base's B, checked, could not be made: its error, with
            # no route and the routes skipped before this one (SPEC-routes).
            trail.forget(LINK_DIR)
            raise
        return _bind_through(call, through, trail, made_in(t))
    if env.config.link_file is not LinkFile.NOT_TRIED:
        done = bind_link_file(call, t, env, bases, trail)
        if done is not None:
            return done
    return bind_thread_cwd_or_at(call, t, env, bases, trail)


def made_in(t: Target) -> bytes:
    """Where link-dir, thread-cwd and at make the socket: the realpath of
    the socket's directory E points to (looked up once, before the route was
    built), and the file name. What they remember (SPEC "remove"): the path
    is not looked up again, so a symlink on the way changed between building
    the route and remembering does not move what is remembered away from
    the socket the bind made."""
    return _join(t.real_dir, t.name)


def _bind_through(call: Call, through: Through, trail: Trail, made: bytes) -> BindDone:
    trail.attempt(through.route)
    try:
        sock = call.plain(through.spelling, where=At(made, made=True))
    except BaseException:
        through.link.discard()
        raise
    return BindDone(sock, through.route, through.linked, through.base, through.link)


def bind_link_file(call: Call, t: Target, env: Env, bases: Bases, trail: Trail) -> BindDone | None:
    """link-file: bind ``<B>/D/E``, E a symlink to the socket file. Where the
    bind was measured to follow it (macOS, NetBSD) a failure is the outcome;
    elsewhere a failure is the outcome only when something is at the path
    afterwards, otherwise the kernel is taken not to have followed the link
    (``None``, noted as skipped, E removed)."""
    try:
        through = link_file_real(env, t, bases)
    except _Unusable as u:
        trail.skip(LINK_FILE, u.why)
        return None
    except OSError:
        trail.forget(LINK_FILE)
        raise
    trail.attempt(LINK_FILE)
    sock = call.new_socket()
    try:
        sock.bind(through.spelling)
    except OSError:
        sock.close()
        through.link.discard()
        # Only the bind's own failure tells whether the kernel followed the
        # symlink; a failure after it (remembering the file) is the outcome.
        if env.config.link_file is LinkFile.FOLLOWS or _something_at(through_target(t)):
            raise
        trail.skip(LINK_FILE, NOT_FOLLOWED)
        return None
    except BaseException:
        sock.close()
        through.link.discard()
        raise
    try:
        # What E points to is looked up after the bind (SPEC "remove"): the
        # path given, made absolute against the cwd.
        call.finish(sock, lambda s: None, where=At(through.target))
    except BaseException:
        through.link.discard()
        raise
    return BindDone(sock, LINK_FILE, through.linked, through.base, through.link)


def through_target(t: Target) -> bytes:
    return absolute(t.path) or t.path


def _something_at(path: bytes) -> bool:
    try:
        os.lstat(path)
    except OSError:
        return False
    return True


def bind_thread_cwd_or_at(call: Call, t: Target, env: Env, bases: Bases, trail: Trail) -> BindDone | None:
    """thread-cwd (Linux, Apple) or at (FreeBSD): E, a random symlink in D
    to the realpath of the socket's directory, bound as ``E/<name>`` from a
    throwaway thread whose cwd is D (or by ``bindat`` on D's fd), and
    removed right after, whatever the outcome (SPEC-routes "thread-cwd と
    at の手順"). ``None`` (noted as skipped) when it cannot be used."""
    if not IS_FREEBSD and not HAS_THREAD_CWD:
        # Not in this system's row (NetBSD, OpenBSD, illumos, ...): not
        # skipped, not tried.
        return None
    route = AT if IS_FREEBSD else THREAD_CWD
    why = None if IS_FREEBSD else thread_cwd_means(env)
    if why is not None:
        trail.skip(route, why)
        return None
    if RANDOM_LEN + 1 + len(t.name) > DIRECT_MAX:
        trail.skip(route, TOO_LONG)
        return None
    b = bases.with_sockdir()
    d = bases.dir(b) if b is not None else None
    if d is None:
        trail.skip(route, NO_D if b is not None else bases.no_b())
        return None
    e = d.random_link(t.real_dir)
    if e is None:
        trail.skip(route, NO_E)
        return None
    spelling = e + b"/" + t.name
    trail.attempt(route)
    try:
        if IS_FREEBSD:
            sock = call.at(d.fd, spelling, where=At(made_in(t), made=True))
        else:
            try:
                sock = thread_cwd_op(call, d.fd, spelling, where=At(made_in(t), made=True))
            except ThreadRefused:
                trail.skip(route, THREAD_CWD_REFUSED)
                return None
    finally:
        d.unlink(e)
    return BindDone(sock, route, Dangling(), None, None)


# ---------- connect ----------


def connect_order(env: Env, proc_ok: bool) -> tuple[str, ...]:
    """The SPEC's connect order past the standard for this system: the
    routes that need neither a write nor a thread (Linux: proc-direct, or
    relative without /proc; Apple: relative, widened; FreeBSD: at-direct;
    elsewhere: relative), then thread-cwd-direct, then link-file."""
    first: tuple[str, ...]
    if IS_LINUX:
        first = (PROC_DIRECT,) if proc_ok else (RELATIVE,)
    elif IS_APPLE:
        first = (RELATIVE, WIDENED)
    elif IS_FREEBSD:
        first = (AT_DIRECT,)
    else:
        first = (RELATIVE,)
    # thread-cwd-direct is in the row only where a thread can be given a
    # cwd of its own (SPEC: a route the system has no means for is not in its
    # row, so not in skipped either).
    return (*first, *((THREAD_CWD_DIRECT,) if HAS_THREAD_CWD else ()), LINK_FILE)


@dataclass
class ConnectCall:
    """One connect of a long path and what its routes share."""

    call: Call
    target: Target
    env: Env
    bases: Bases
    trail: Trail
    proc_ok: bool
    wide: bool
    route: str = ""
    """The route being tried."""
    relative_made: bool = False
    relative_spelling: Relative | None = None
    _dirfd: int | None = None
    _dir_opened: bool = False

    @property
    def dirfd(self) -> int | None:
        """The socket's directory, opened when a route that needs its fd
        first asks (SPEC-routes: only those routes open it); ``None`` when it
        cannot be opened."""
        if not self._dir_opened:
            self._dir_opened = True
            try:
                self._dirfd = open_dir(self.target.directory)
            except OSError:
                self._dirfd = None
        return self._dirfd

    def close(self) -> None:
        if self._dirfd is not None:
            os.close(self._dirfd)
            self._dirfd = None

    def attempt(self) -> None:
        """Note the route as the one whose connect runs now (its failure is
        then the call's, with this route)."""
        self.trail.attempt(self.route)

    def relative(self) -> Relative | None:
        if not self.relative_made:
            self.relative_made = True
            self.relative_spelling = Relative.new(self.target.path, self.target.directory)
        return self.relative_spelling


def connect_long(
    call: Call, t: Target, env: Env, bases: Bases, trail: Trail, order: tuple[str, ...] | None = None
) -> tuple[socket.socket, str]:
    """Connect by the SPEC's order for this system (or ``order``, which the
    tests give to force a route), the first route that can be used; its
    connect's failure is the outcome. Past the last: ``ENAMETOOLONG``."""
    trail.skip(STANDARD, "path too long")
    proc_ok = IS_LINUX and proc_usable(env, pid=False)
    cc = ConnectCall(call, t, env, bases, trail, proc_ok, IS_APPLE and env.config.wide)
    try:
        for route in order if order is not None else connect_order(env, proc_ok):
            cc.route = route
            outcome = CONNECT_ROUTES[route](cc)
            if isinstance(outcome, socket.socket):
                return outcome, route
            trail.skip(route, outcome)
        raise enametoolong()
    finally:
        cc.close()


def connect_proc_direct(cc: ConnectCall) -> socket.socket | str:
    """``/proc/self/fd/<fd of the socket's directory>/<name>`` (Linux)."""
    if not cc.proc_ok:
        return PROC_UNUSABLE
    if cc.dirfd is None:
        return SOCKET_DIR_NOT_OPENED
    proc = cc.env.config.proc
    assert proc is not None
    fd_dir = b"%s/self/fd/%d" % (proc, cc.dirfd)
    # /proc must name the socket's directory itself: the same (st_dev,
    # st_ino) as its fd (a /proc of another PID namespace or mount would send
    # the connect to another directory's file of that name).
    try:
        via_st, held = os.stat(fd_dir), os.fstat(cc.dirfd)
    except OSError:
        return PROC_UNUSABLE
    if (via_st.st_dev, via_st.st_ino) != (held.st_dev, held.st_ino):
        return PROC_UNUSABLE
    via = fd_dir + b"/" + cc.target.name
    if len(via) > DIRECT_MAX:
        return TOO_LONG
    cc.attempt()
    return cc.call.plain(via)


def connect_relative(cc: ConnectCall) -> socket.socket | str:
    """The path spelled relative to the cwd (:class:`Relative`)."""
    rel = cc.relative()
    if rel is None or len(rel.path) > DIRECT_MAX:
        return "no relative spelling fits"
    cc.attempt()
    return rel.run(lambda p: cc.call.plain(p), cc.call.sock)


def connect_widened(cc: ConnectCall) -> socket.socket | str:
    """The path itself, or else its relative spelling, in a widened
    ``sockaddr_un`` (Apple)."""
    if not cc.wide:
        return "widened sockaddr unusable"
    try:
        if len(cc.target.path) <= WIDE_PATH_MAX:
            cc.attempt()
            return cc.call.wide(cc.target.path)
        rel = cc.relative()
        if rel is not None and len(rel.path) <= WIDE_PATH_MAX:
            cc.attempt()
            return rel.run(lambda p: cc.call.wide(p), cc.call.sock)
    except OSError as e:
        if not refuses_wide(e):
            raise
        cc.wide = False
        return "widened sockaddr refused"
    return TOO_LONG


def refuses_wide(e: OSError) -> bool:
    """Whether an error from a widened-address call means the kernel does not
    take the widened form (rather than a real outcome such as ``ENOENT``).
    XNU answers ``EINVAL``, or ``ENAMETOOLONG`` past ``SOCK_MAXADDRLEN``."""
    return e.errno in (errno.EINVAL, errno.ENAMETOOLONG)


def connect_at_direct(cc: ConnectCall) -> socket.socket | str:
    """``connectat(2)`` of the file name on the socket's directory's fd
    (FreeBSD)."""
    if cc.dirfd is None:
        return SOCKET_DIR_NOT_OPENED
    if len(cc.target.name) > DIRECT_MAX:
        return TOO_LONG
    cc.attempt()
    return cc.call.at(cc.dirfd, cc.target.name)


def connect_thread_cwd_direct(cc: ConnectCall) -> socket.socket | str:
    """The file name from a throwaway thread whose cwd is the socket's
    directory (Linux, Apple)."""
    why = thread_cwd_means(cc.env)
    if why is not None:
        return why
    if cc.dirfd is None:
        return SOCKET_DIR_NOT_OPENED
    if len(cc.target.name) > DIRECT_MAX:
        return TOO_LONG
    cc.attempt()
    try:
        return thread_cwd_op(cc.call, cc.dirfd, cc.target.name)
    except ThreadRefused:
        cc.trail.trying = None
        return THREAD_CWD_REFUSED


def connect_link_file(cc: ConnectCall) -> socket.socket | str:
    """A random symlink E right in B to the socket file, connected as
    ``<B's real path>/E`` and removed right after, whatever the outcome (no
    D, nothing left in the process). B is not made when the spelling cannot
    fit through any place."""
    if cc.bases.given is not None:
        given_len = cc.env.given_len(cc.bases.given)
        if given_len is None:
            return cc.bases.no_b()
        if given_len + 1 + RANDOM_LEN > DIRECT_MAX:
            return TOO_LONG
    b = cc.bases.plain(lambda n: n + 1 + RANDOM_LEN <= DIRECT_MAX)
    if b is None:
        if cc.bases.too_long_seen():
            return cc.bases.too_long()
        return cc.bases.no_b()
    if len(b.real) + 1 + RANDOM_LEN > DIRECT_MAX:
        return TOO_LONG
    target = absolute(cc.target.path)
    if target is None:
        return "cwd unreadable"
    e = b.random_link(target)
    if e is None:
        return NO_E
    cc.attempt()
    try:
        return cc.call.plain(_join(b.real, e))
    finally:
        b.unlink(e)


CONNECT_ROUTES: dict[str, Callable[[ConnectCall], socket.socket | str]] = {
    PROC_DIRECT: connect_proc_direct,
    RELATIVE: connect_relative,
    WIDENED: connect_widened,
    AT_DIRECT: connect_at_direct,
    THREAD_CWD_DIRECT: connect_thread_cwd_direct,
    LINK_FILE: connect_link_file,
}
"""What each route of a connect does: the socket it connected, or why it
cannot be used."""


# ---------- relative ----------


def _dir_id(directory: bytes) -> tuple[int, int] | None:
    try:
        st = os.stat(directory)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _cwd_spellings(cwd: bytes, cwd_id: tuple[int, int]) -> list[bytes]:
    """The spellings of the cwd the path may start with: ``getcwd(3)``'s, and
    ``$PWD`` but only when it names the cwd (``stat`` agrees)."""
    spellings = [cwd]
    pwd = os.environb.get(b"PWD")
    if pwd and pwd.startswith(b"/") and pwd != cwd and _dir_id(pwd) == cwd_id:
        spellings.append(pwd)
    return spellings


SPELLING_MAX = WIDE_PATH_MAX if IS_APPLE else DIRECT_MAX
"""The longest spelling a connect's sockaddr carries here (widened on
Apple)."""


@dataclass(frozen=True)
class Relative:
    """A relative spelling of an absolute socket path, checked to name the
    same directory (SPEC-routes "相対パスの作り方")."""

    path: bytes
    cwd: tuple[int, int] = field(repr=False)

    @classmethod
    def new(cls, path: bytes, directory: bytes) -> Relative | None:
        """1. Cut a spelling of the cwd (``getcwd``, or a ``$PWD`` naming
        it) off the front, component by component. 2. Only when that gives
        nothing that fits: climb out of the cwd with ``..`` along real
        paths. 3. The shorter is kept, and used only when its directory is
        the socket's directory (``(st_dev, st_ino)``) and the cwd has not
        changed meanwhile."""
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

    def run(self, f: Callable[[bytes], socket.socket], keep: socket.socket | None = None) -> socket.socket:
        """Run ``f`` with the relative spelling, then confirm the cwd is still
        the one it was made against; if not, :class:`CwdChanged` is raised and
        the socket closed (``keep``, the caller's socket of a datagram
        connect, is left to the caller)."""
        result: socket.socket | None = None
        failure: OSError | None = None
        try:
            result = f(self.path)
        except OSError as e:
            failure = e
        if _dir_id(b".") != self.cwd:
            if result is not None and result is not keep:
                result.close()
            raise CwdChanged(
                "the current directory changed while connecting through a path relative to it; the result was discarded"
            ) from failure
        if failure is not None:
            raise failure
        assert result is not None
        return result
