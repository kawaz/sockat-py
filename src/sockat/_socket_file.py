"""The socket file a bind made and the E its spelling goes through, kept so
that they can be removed later without removing anything else (the hub
SPEC's ``remove`` and ``cleanup``)."""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import threading

from ._errors import CwdChanged
from ._place import Dir
from ._sys import IS_LINUX


class Link:
    """One E of a resolve (in D), removed once by :meth:`cleanup` (asked for
    by the caller) or :meth:`discard` (inside a call)."""

    def __init__(self, d: Dir, name: bytes, *, kept: bool = False) -> None:
        self._d = d
        self._name = name
        self._kept = kept
        """The default bind's link-dir E, used again: never removed inside a
        call, even by the one that made it (another default bind may be
        using it; left, the next bind uses it. SPEC-routes)."""
        self._done = False
        self.lock = threading.RLock()
        """Held while E (and, by every :class:`SocketFile` sharing this E, the
        socket file) is removed: one removal at a time across threads, among
        a ``ForBind`` and every ``SocketFile`` its ``socket_file()`` gave."""

    def cleanup(self) -> None:
        """Remove E, as the caller asked: a failure other than E being gone
        is raised (SPEC "remove と cleanup"), and a later call tries again;
        once it has succeeded, later calls do nothing."""
        with self.lock:
            if self._done:
                return
            self._d.unlink_or_raise(self._name)
            self._done = True

    def discard(self) -> None:
        """Remove E inside a call (its bind failed): a failure is passed over
        in silence (SPEC-routes "経路の失敗の扱い")."""
        with self.lock:
            if self._done:
                return
            self._done = True
            if self._kept:
                return
            self._d.unlink(self._name)


class SocketFile:
    """The socket file a bind made (``Bound.file``): :meth:`remove` removes
    it and :meth:`cleanup` the E of its spelling, each once and only when
    called (never when the listener is closed or collected)."""

    def __init__(self, file: tuple[bytes, tuple[int, int]] | None, link: Link | None) -> None:
        self._file = file
        self._link = link
        self._removed = False
        # One removal at a time, also among the SocketFile sharing an E (the
        # ones a ForBind's socket_file() gives): the E's lock (reentrant, as
        # remove() goes on to cleanup()).
        self._lock = link.lock if link is not None else threading.RLock()

    def remove(self) -> None:
        """Remove the socket file the bind made, and :meth:`cleanup`. It does
        not close the listener (close it yourself).

        The realpath of the socket file and its ``(st_dev, st_ino)`` were
        remembered right after the bind; the file at that realpath is removed
        only while it is still that file. Gone already, or replaced by
        another: nothing is removed, and it succeeds. A replacement is told
        apart while the listener is open (the kernel holds the file meanwhile,
        so its number is not reused); one removed and made again after the
        listener was closed may get the same numbers on a file system that
        reuses them at once (ext4, BSD's UFS / FFS). So call it before closing
        the listener. Once it has succeeded, later calls do nothing.

        Raises :class:`OSError` when the file cannot be looked up (other than
        it being gone) or removing it fails (and a later call tries again);
        then :meth:`cleanup` is still done, and its own failure, when the
        file's went well, is raised instead."""
        with self._lock:
            if self._removed:
                self.cleanup()
                return
            file_error: OSError | None = None
            try:
                if self._file is not None:
                    _remove_if_same(*self._file)
                self._removed = True
            except OSError as e:
                file_error = e
            # cleanup() goes on whatever happened to the file; the file's error
            # comes first, cleanup()'s when there is none (SPEC).
            try:
                self.cleanup()
            except OSError as e:
                if file_error is None:
                    raise
                file_error.add_note(f"cleaning up the symlink also failed: {e}")
            if file_error is not None:
                raise file_error

    def cleanup(self) -> None:
        """Remove the E the bind's spelling goes through (link-dir,
        link-file). The spelling names nothing afterwards: do not use it
        again. A failure other than E being gone is raised (a later call
        tries again); once it has succeeded, later calls do nothing."""
        if self._link is not None:
            self._link.cleanup()


def remember_spelling(spelling: bytes, cwd: tuple[int, int] | None, link: Link | None) -> SocketFile:
    """The :class:`SocketFile` of what a spelling the caller bound names now
    (SPEC "ForBind の SocketFile"): the spelling is followed to the socket
    file's realpath and ``(st_dev, st_ino)``. It must be there (bound
    already): otherwise the error. A relative spelling (``cwd`` given, the
    cwd's when it was resolved) is refused when the cwd has changed
    since."""
    if IS_LINUX and spelling.startswith(b"\0"):
        return SocketFile(None, link)
    if cwd is not None and cwd_id() != cwd:
        raise CwdChanged("the current directory changed since the path relative to it was resolved")
    real = os.fsencode(os.path.realpath(spelling, strict=True))
    st = os.lstat(real)
    if not stat.S_ISSOCK(st.st_mode):
        # Not bound yet: an unrelated file at the path, which remove() must
        # never remove.
        raise OSError(errno.ENOTSOCK, os.strerror(errno.ENOTSOCK), os.fsdecode(real))
    return SocketFile((real, (st.st_dev, st.st_ino)), link)


def _remove_if_same(real: bytes, made: tuple[int, int]) -> None:
    try:
        now = os.lstat(real)
    except (FileNotFoundError, NotADirectoryError):
        return
    if (now.st_dev, now.st_ino) != made:
        return
    with contextlib.suppress(FileNotFoundError):
        os.unlink(real)


class Recorder:
    """What ``sockat.ex``'s binds remember: the path given, for a relative
    one the cwd it was given against, and, once the bind has gone through,
    the socket file it made."""

    def __init__(self, path: bytes) -> None:
        self.path = path
        self.abstract = IS_LINUX and path.startswith(b"\0")
        self.relative = not path.startswith(b"/") and not self.abstract
        self.cwd = cwd_id() if self.relative else None
        """``(st_dev, st_ino)`` of the cwd before anything is looked up, for
        a relative path."""
        self.file: tuple[bytes, tuple[int, int]] | None = None

    def bound(self, made: bytes | None = None) -> None:
        """Remember the socket file just bound (before the listen and before
        the socket is returned), so that a file put at the path afterwards
        cannot be remembered in its place. A failure is raised (the caller
        closes the socket: a ``remove`` that could not remove the file must
        not look like one that can). For a relative path, a cwd changed
        since the call began is such a failure (:class:`CwdChanged`).

        ``made``: where the route made the socket (link-dir, thread-cwd, at:
        the real directory E points to and the file name), remembered as it
        is; the path is not looked up again (SPEC "remove"). ``None`` (the
        standard, link-file): the realpath of the path, looked up now."""
        if self.abstract:
            return
        failure: OSError | None = None
        try:
            # realpath follows a symlink in the last component too: where the
            # bind followed one (macOS, NetBSD), the socket is where it
            # points, and that is what is remembered (remove() must not
            # remove the caller's symlink). What is there must be a socket.
            if made is None:
                real = os.fsencode(os.path.realpath(self.path, strict=True))
            else:
                real = made
                if stat.S_ISLNK(os.lstat(made).st_mode):
                    real = os.fsencode(os.path.realpath(made, strict=True))
            st = os.lstat(real)
            if not stat.S_ISSOCK(st.st_mode):
                raise OSError(errno.ENOTSOCK, os.strerror(errno.ENOTSOCK), os.fsdecode(real))
        except OSError as e:
            failure = e
        # A relative path: the cwd is compared now, after the bind and the
        # realpath, with the one before anything was looked up (SPEC). Another
        # thread that changed it in between would have the bind made under one
        # cwd and the file remembered under another, and remove() could then
        # remove an unrelated file of the same name. A change undone before
        # this point is not seen.
        if self.relative and (self.cwd is None or cwd_id() != self.cwd):
            raise CwdChanged(
                "the current directory changed while binding a path relative to it; the socket was closed"
            ) from failure
        if failure is not None:
            raise failure
        self.file = (real, (st.st_dev, st.st_ino))


def cwd_id() -> tuple[int, int] | None:
    try:
        st = os.stat(".")
    except OSError:
        return None
    return st.st_dev, st.st_ino
