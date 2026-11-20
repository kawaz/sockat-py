"""The routes to a long socket path and the order they are tried in (the hub
SPEC's "長いパスの経路と試す順番" and SPEC-routes). The public calls go
through :func:`bind` / :func:`connect` / :func:`resolve_for_bind` /
:func:`resolve_for_connect`; each route is a module-level function so the
tests can force it."""

from __future__ import annotations

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
from ._place import RANDOM_LEN, Base, Env, Kind, LinkFile, avoids_shared_tmp, open_dir
from ._socket_file import Link, Recorder
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

TOO_LONG = "spelling too long"
PROC_UNUSABLE = "/proc unusable"
NO_PLACE_FOR_B = "no place for B"
NO_CROSS_UID_B = "no B other uids can follow"
NO_D = "D not made"
NO_E = "E not made"
NO_MEANS = "no means on this system"
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

    def finish(self, sock: socket.socket, do: Callable[[socket.socket], None]) -> socket.socket:
        """Run ``do`` on ``sock``, then after a bind remember the file and
        listen (stream); on any failure close ``sock`` when it is not the
        caller's."""
        try:
            do(sock)
            if self.op is Op.BIND:
                if self.recorder is not None:
                    self.recorder.bound()
                if self.listens:
                    sock.listen()
        except BaseException:
            if sock is not self.sock:
                sock.close()
            raise
        return sock

    def plain(self, spelling: str | bytes, *, standard: bool = False) -> socket.socket:
        """Bind / connect with ``spelling`` through the ``socket`` module."""
        sock = self.new_socket(standard=standard)
        if self.op is Op.BIND:
            return self.finish(sock, lambda s: s.bind(spelling))
        return self.finish(sock, lambda s: s.connect(spelling))

    def wide(self, spelling: bytes) -> socket.socket:
        """Connect with ``spelling`` in a widened ``sockaddr_un`` (Apple)."""
        addr = sockaddr(spelling, wide=True)
        return self.finish(self.new_socket(), lambda s: op(self.op, s, addr))

    def at(self, dirfd: int, spelling: bytes) -> socket.socket:
        """``bindat(2)`` / ``connectat(2)`` of ``spelling`` on ``dirfd``
        (FreeBSD)."""
        addr = sockaddr(spelling)
        return self.finish(self.new_socket(), lambda s: op_at(self.op, s, dirfd, addr))


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


def look_up(path: bytes) -> Target:
    """The SPEC's steps before any route: a path that does not end with a
    file name is an argument error; the socket's directory is looked up
    (``ENOENT``, ``ENOTDIR``) and resolved to its realpath (its error), all
    before any length is judged or anything is made."""
    check_no_nul(path)
    directory, name = _path.split(path)
    st = os.stat(directory)
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), os.fsdecode(directory))
    real_dir = os.fsencode(os.path.realpath(directory, strict=True))
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


def proc_usable(env: Env, fd: int | None = None) -> bool:
    """Whether /proc lists this process's fds, both as ``/proc/self/fd/<fd>``
    and ``/proc/<pid>/fd/<fd>`` (looked up for ``fd``, or for an fd of ``/``
    opened for the purpose)."""
    proc = env.config.proc
    if proc is None:
        return False
    probe = fd
    if probe is None:
        try:
            probe = open_dir(b"/")
        except OSError:
            return False
    try:
        for who in (b"self", b"%d" % os.getpid()):
            try:
                os.stat(b"%s/%s/fd/%d" % (proc, who, probe))
            except OSError:
                return False
        return True
    finally:
        if fd is None:
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

    def plain(self) -> Base | None:
        """The process's B (``Options.base`` when given, raising what fails),
        not the one in the socket's directory."""
        if not self._plain_tried:
            self._plain_tried = True
            if self.given is not None:
                self._plain = self.env.given_base(self.given, cross_uid=self.cross_uid)
            else:
                self._plain = self.env.auto_base()
        return self._plain

    def cross(self) -> Base | None:
        """B another uid can follow a spelling through (``Options.base`` when
        given: checked so)."""
        if not self._cross_tried:
            self._cross_tried = True
            if self.given is not None:
                self._cross = self.env.given_base(self.given, cross_uid=True)
            else:
                self._cross = self.env.cross_base()
        return self._cross

    def for_real_path(self) -> Base | None:
        """B for a spelling by B's real path."""
        return self.cross() if self.cross_uid else self.plain()

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


class _Unusable(Exception):
    """A route cannot be used (``why``); internal to the routes."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def _proc_len(env: Env, b: Base | None, fd_digits: int) -> int:
    proc = env.config.proc or b""
    fd_part = len(str(b.fd)) if b is not None else fd_digits
    return len(b"%s/%d/fd/" % (proc, os.getpid())) + fd_part


def link_dir_proc(env: Env, t: Target, bases: Bases) -> Through:
    """link-dir with procB: ``/proc/<pid>/fd/<B's fd>/D/E/<name>``, E a
    symlink in D to the realpath of the socket's directory."""
    if bases.cross_uid:
        raise _Unusable("procB not followed by other uids")
    if not proc_usable(env):
        raise _Unusable(PROC_UNUSABLE)
    # The length is judged before anything is made: E's name is at least
    # one character.
    tail = 1 + RANDOM_LEN + 1 + 1 + 1 + len(t.name)
    b = bases.plain()
    if b is None and bases.given is None:
        if _proc_len(env, None, _fd_digits_guess()) + tail > DIRECT_MAX:
            raise _Unusable(TOO_LONG)
        b = bases.with_sockdir()
    if b is None:
        raise _Unusable(NO_PLACE_FOR_B)
    proc = env.config.proc
    assert proc is not None
    head = b.proc(proc, os.getpid())
    if len(head) + tail > DIRECT_MAX:
        raise _Unusable(TOO_LONG)
    made = _link_in_d(env, b, head, t.real_dir, t.name)
    kind = "sockdir" if b.kind is Kind.SOCKDIR else "proc"
    return Through(
        made[0],
        LINK_DIR,
        kind,
        made[1],
        Linked(removable=True, until=Until.PROCESS, usable_cross_uid=False, usable_with_private_tmp=True),
    )


def link_dir_real(env: Env, t: Target, bases: Bases) -> Through:
    """link-dir with B's real path: ``<B>/D/E/<name>``."""
    b = bases.for_real_path()
    if b is None:
        raise _Unusable(NO_CROSS_UID_B if bases.cross_uid else NO_PLACE_FOR_B)
    if len(b.real) + 1 + RANDOM_LEN + 1 + 1 + 1 + len(t.name) > DIRECT_MAX:
        raise _Unusable(TOO_LONG)
    spelling, link = _link_in_d(env, b, b.real, t.real_dir, t.name)
    return Through(
        spelling,
        LINK_DIR,
        b.kind.value,
        link,
        Linked(
            removable=True,
            until=Until.CLEANUP,
            usable_cross_uid=b.cross_uid,
            usable_with_private_tmp=avoids_shared_tmp(spelling),
        ),
    )


def link_file_real(env: Env, t: Target, bases: Bases) -> Through:
    """link-file with B's real path: ``<B>/D/E``, E a symlink in D to the
    socket file (the path made absolute against the cwd)."""
    b = bases.for_real_path()
    if b is None:
        raise _Unusable(NO_CROSS_UID_B if bases.cross_uid else NO_PLACE_FOR_B)
    if len(b.real) + 1 + RANDOM_LEN + 1 + 1 > DIRECT_MAX:
        raise _Unusable(TOO_LONG)
    target = absolute(t.path)
    if target is None:
        raise _Unusable("cwd unreadable")
    spelling, link = _link_in_d(env, b, b.real, target, None)
    return Through(
        spelling,
        LINK_FILE,
        b.kind.value,
        link,
        Linked(
            removable=False,
            until=Until.CLEANUP,
            usable_cross_uid=b.cross_uid,
            usable_with_private_tmp=avoids_shared_tmp(spelling),
        ),
    )


def link_file_proc(env: Env, t: Target, bases: Bases) -> Through:
    """link-file with procB: ``/proc/<pid>/fd/<B's fd>/D/E`` (only
    ``resolve_for_connect``: a bind does not follow it where /proc is)."""
    if bases.cross_uid:
        raise _Unusable("procB not followed by other uids")
    if not proc_usable(env):
        raise _Unusable(PROC_UNUSABLE)
    tail = 1 + RANDOM_LEN + 1 + 1
    b = bases.plain()
    if b is None and bases.given is None:
        if _proc_len(env, None, _fd_digits_guess()) + tail > DIRECT_MAX:
            raise _Unusable(TOO_LONG)
        b = bases.with_sockdir()
    if b is None:
        raise _Unusable(NO_PLACE_FOR_B)
    proc = env.config.proc
    assert proc is not None
    head = b.proc(proc, os.getpid())
    if len(head) + tail > DIRECT_MAX:
        raise _Unusable(TOO_LONG)
    target = absolute(t.path)
    if target is None:
        raise _Unusable("cwd unreadable")
    spelling, link = _link_in_d(env, b, head, target, None)
    kind = "sockdir" if b.kind is Kind.SOCKDIR else "proc"
    return Through(
        spelling,
        LINK_FILE,
        kind,
        link,
        Linked(removable=False, until=Until.PROCESS, usable_cross_uid=False, usable_with_private_tmp=True),
    )


def _link_in_d(env: Env, b: Base, head: bytes, target: bytes, name: bytes | None) -> tuple[bytes, Link]:
    """E in D of ``b`` pointing to ``target``, and the spelling
    ``<head>/D/E[/<name>]`` (checked again to fit, E's name now known)."""
    d = b.dir(env.config.uid)
    if d is None:
        raise _Unusable(NO_D)
    e = d.link(target)
    if e is None:
        raise _Unusable(NO_E)
    spelling = _join(_join(head, d.name), e)
    if name is not None:
        spelling = _join(spelling, name)
    if len(spelling) > DIRECT_MAX:
        d.unlink(e)
        raise _Unusable(TOO_LONG)
    return spelling, Link(d, e)


# ---------- the throwaway thread ----------


class ThreadRefused(Exception):
    """The system will not give a thread a cwd of its own."""


def in_thread_cwd(dirfd: int, do: Callable[[], None]) -> None:
    """Run ``do`` in a throwaway :class:`threading.Thread` whose own cwd is
    the directory ``dirfd`` (SPEC-routes "使い捨てのスレッド"): Linux
    ``unshare(CLONE_FS)`` then ``fchdir``, Apple ``pthread_fchdir_np``. The
    process cwd and the calling thread's never change; the thread ends with
    the call. :class:`ThreadRefused` when the thread cannot be started or
    the system refuses it a cwd of its own; any other failure of ``do`` (or
    of the ``fchdir`` after ``unshare``) is raised.

    Whatever interrupts the wait (``KeyboardInterrupt`` out of ``join``), the
    thread is waited for to the end before this returns or raises, so that
    the caller never closes ``dirfd`` (or a socket) while the thread may
    still use it."""
    outcome: list[BaseException | None] = []
    refused = ThreadRefused()

    def run() -> None:
        try:
            if not enter_dir_in_this_thread(dirfd):
                outcome.append(refused)
                return
            do()
            outcome.append(None)
        except BaseException as e:
            outcome.append(e)

    thread = threading.Thread(target=run, name="sockat-thread-cwd")
    try:
        thread.start()
    except RuntimeError:
        raise ThreadRefused() from None
    interrupted: BaseException | None = None
    while True:
        try:
            thread.join()
            break
        except BaseException as e:
            if interrupted is None:
                interrupted = e
    if interrupted is not None:
        raise interrupted
    result = outcome[0] if outcome else RuntimeError("the thread-cwd thread ended without an outcome")
    if result is not None:
        raise result


def thread_cwd_op(call: Call, dirfd: int, spelling: bytes) -> socket.socket:
    """Bind / connect ``spelling`` from a throwaway thread whose cwd is
    ``dirfd`` (:func:`in_thread_cwd`); then, in the caller, what follows a
    bind (remember, listen)."""
    sock = call.new_socket()
    try:
        if call.op is Op.BIND:
            in_thread_cwd(dirfd, lambda: sock.bind(spelling))
        else:
            in_thread_cwd(dirfd, lambda: sock.connect(spelling))
    except BaseException:
        if sock is not call.sock:
            sock.close()
        raise
    return call.finish(sock, lambda s: None)


def thread_cwd_means(env: Env) -> str | None:
    """Why a thread's own cwd cannot be used here (``None``: it may be)."""
    if not env.config.thread_cwd or not (IS_LINUX or IS_APPLE):
        return NO_MEANS
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


def bind_standard(call: Call, spelled: str | bytes, raw: bytes) -> BindDone:
    sock = call.plain(spelled, standard=True)
    absolute_path = raw.startswith(b"/") or (IS_LINUX and raw.startswith(b"\0"))
    return BindDone(sock, STANDARD, Original() if absolute_path else CwdRelative(), None, None)


def bind_long(call: Call, t: Target, env: Env, bases: Bases, trail: Trail) -> BindDone:
    """The SPEC's bind order past the standard: link-dir (procB, then B's
    real path), link-file (where a bind may follow the symlink),
    thread-cwd / at, then ``ENAMETOOLONG``."""
    trail.skip(STANDARD, "path too long")
    for make in (link_dir_proc, link_dir_real):
        try:
            through = make(env, t, bases)
        except _Unusable as u:
            trail.skip(LINK_DIR, u.why)
            continue
        return _bind_through(call, through, trail)
    if env.config.link_file is not LinkFile.NOT_TRIED:
        done = bind_link_file(call, t, env, bases, trail)
        if done is not None:
            return done
    done = bind_thread_cwd_or_at(call, t, env, bases, trail)
    if done is not None:
        return done
    raise enametoolong()


def _bind_through(call: Call, through: Through, trail: Trail) -> BindDone:
    trail.attempt(through.route)
    try:
        sock = call.plain(through.spelling)
    except BaseException:
        through.link.cleanup()
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
    trail.attempt(LINK_FILE)
    try:
        sock = call.plain(through.spelling)
    except OSError:
        through.link.cleanup()
        if env.config.link_file is LinkFile.FOLLOWS or _something_at(through_target(t)):
            raise
        trail.skip(LINK_FILE, NOT_FOLLOWED)
        return None
    except BaseException:
        through.link.cleanup()
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
    route = AT if IS_FREEBSD else THREAD_CWD
    why = None if IS_FREEBSD else thread_cwd_means(env)
    if why is not None:
        trail.skip(route, why)
        return None
    if RANDOM_LEN + 1 + len(t.name) > DIRECT_MAX:
        trail.skip(route, TOO_LONG)
        return None
    b = bases.with_sockdir()
    d = b.dir(env.config.uid) if b is not None else None
    if d is None:
        trail.skip(route, NO_D if b is not None else NO_PLACE_FOR_B)
        return None
    e = d.random_link(t.real_dir)
    if e is None:
        trail.skip(route, NO_E)
        return None
    spelling = e + b"/" + t.name
    trail.attempt(route)
    try:
        if IS_FREEBSD:
            sock = call.at(d.fd, spelling)
        else:
            try:
                sock = thread_cwd_op(call, d.fd, spelling)
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
    return (*first, THREAD_CWD_DIRECT, LINK_FILE)


@dataclass
class ConnectCall:
    """One connect of a long path and what its routes share."""

    call: Call
    target: Target
    env: Env
    bases: Bases
    dirfd: int | None
    proc_ok: bool
    wide: bool
    relative_made: bool = False
    relative_spelling: Relative | None = None

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
    try:
        dirfd: int | None = open_dir(t.directory)
    except OSError:
        dirfd = None
    try:
        proc_ok = IS_LINUX and proc_usable(env, dirfd)
        cc = ConnectCall(call, t, env, bases, dirfd, proc_ok, IS_APPLE and env.config.wide)
        for route in order if order is not None else connect_order(env, proc_ok):
            trail.attempt(route)
            outcome = CONNECT_ROUTES[route](cc)
            if isinstance(outcome, socket.socket):
                return outcome, route
            trail.skip(route, outcome)
        raise enametoolong()
    finally:
        if dirfd is not None:
            os.close(dirfd)


def connect_proc_direct(cc: ConnectCall) -> socket.socket | str:
    """``/proc/self/fd/<fd of the socket's directory>/<name>`` (Linux)."""
    if not cc.proc_ok:
        return PROC_UNUSABLE
    if cc.dirfd is None:
        return SOCKET_DIR_NOT_OPENED
    proc = cc.env.config.proc
    assert proc is not None
    via = b"%s/self/fd/%d/%s" % (proc, cc.dirfd, cc.target.name)
    if len(via) > DIRECT_MAX:
        return TOO_LONG
    return cc.call.plain(via)


def connect_relative(cc: ConnectCall) -> socket.socket | str:
    """The path spelled relative to the cwd (:class:`Relative`)."""
    rel = cc.relative()
    if rel is None or len(rel.path) > DIRECT_MAX:
        return "no relative spelling fits"
    return rel.run(lambda p: cc.call.plain(p), cc.call.sock)


def connect_widened(cc: ConnectCall) -> socket.socket | str:
    """The path itself, or else its relative spelling, in a widened
    ``sockaddr_un`` (Apple)."""
    if not cc.wide:
        return "widened sockaddr unusable"
    try:
        if len(cc.target.path) <= WIDE_PATH_MAX:
            return cc.call.wide(cc.target.path)
        rel = cc.relative()
        if rel is not None and len(rel.path) <= WIDE_PATH_MAX:
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
    try:
        return thread_cwd_op(cc.call, cc.dirfd, cc.target.name)
    except ThreadRefused:
        return THREAD_CWD_REFUSED


def connect_link_file(cc: ConnectCall) -> socket.socket | str:
    """A random symlink E right in B to the socket file, connected as
    ``<B's real path>/E`` and removed right after, whatever the outcome (no
    D, nothing left in the process)."""
    b = cc.bases.plain()
    if b is None:
        return NO_PLACE_FOR_B
    if len(b.real) + 1 + RANDOM_LEN > DIRECT_MAX:
        return TOO_LONG
    target = absolute(cc.target.path)
    if target is None:
        return "cwd unreadable"
    e = b.random_link(target)
    if e is None:
        return NO_E
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
