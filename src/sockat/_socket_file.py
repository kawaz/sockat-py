"""The socket file a bind made and the E its spelling goes through, kept so
that they can be removed later without removing anything else (the hub
SPEC's ``remove`` and ``cleanup``)."""

from __future__ import annotations

import contextlib
import os
import threading

from ._errors import CwdChanged
from ._place import Dir
from ._sys import IS_LINUX


class Link:
    """One E of a resolve (in D), removed once by :meth:`cleanup`."""

    def __init__(self, d: Dir, name: bytes) -> None:
        self._d = d
        self._name = name
        self._done = False
        self._lock = threading.Lock()

    def cleanup(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        self._d.unlink(self._name)


class SocketFile:
    """The socket file a bind made (``Bound.file``): :meth:`remove` removes
    it and :meth:`cleanup` the E of its spelling, each once and only when
    called (never when the listener is closed or collected)."""

    def __init__(self, file: tuple[bytes, tuple[int, int]] | None, link: Link | None) -> None:
        self._file = file
        self._link = link
        self._removed = False
        self._lock = threading.Lock()

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
        it being gone) or removing it fails."""
        with self._lock:
            if self._removed:
                return
            try:
                if self._file is not None:
                    _remove_if_same(*self._file)
                self._removed = True
            finally:
                self.cleanup()

    def cleanup(self) -> None:
        """Remove the E the bind's spelling goes through (link-dir,
        link-file), once. The spelling names nothing afterwards: do not use
        it again."""
        if self._link is not None:
            self._link.cleanup()


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
        self.cwd = _cwd_id() if self.relative else None
        """``(st_dev, st_ino)`` of the cwd before anything is looked up, for
        a relative path."""
        self.file: tuple[bytes, tuple[int, int]] | None = None

    def bound(self) -> None:
        """Remember the socket file just bound (before the listen and before
        the socket is returned), so that a file put at the path afterwards
        cannot be remembered in its place. A failure is raised (the caller
        closes the socket: a ``remove`` that could not remove the file must
        not look like one that can). For a relative path, a cwd changed
        since the call began is such a failure (:class:`CwdChanged`)."""
        if self.abstract:
            return
        failure: OSError | None = None
        try:
            real = os.fsencode(os.path.realpath(self.path, strict=True))
            st = os.lstat(real)
        except OSError as e:
            failure = e
        # A relative path: the cwd is compared now, after the bind and the
        # realpath, with the one before anything was looked up (SPEC). Another
        # thread that changed it in between would have the bind made under one
        # cwd and the file remembered under another, and remove() could then
        # remove an unrelated file of the same name. A change undone before
        # this point is not seen.
        if self.relative and (self.cwd is None or _cwd_id() != self.cwd):
            raise CwdChanged(
                "the current directory changed while binding a path relative to it; the socket was closed"
            ) from failure
        if failure is not None:
            raise failure
        self.file = (real, (st.st_dev, st.st_ino))


def _cwd_id() -> tuple[int, int] | None:
    try:
        st = os.stat(".")
    except OSError:
        return None
    return st.st_dev, st.st_ino
