"""The socket file a bind made, remembered so that it can be removed later
without removing anything else (:func:`sockat.ex.bind`, the SPEC's ``_ex``)."""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable, Iterator
from contextvars import ContextVar

from ._errors import CwdChanged


class _Recorder:
    """What :func:`sockat.ex.bind` asks the bind to remember: the path given,
    for a relative one the cwd it was given against, and, once the bind has
    gone through, ``discard`` for the file it made."""

    def __init__(self, path: str | bytes) -> None:
        self.path = path
        self.relative = _relative(path)
        self.cwd = _cwd_id() if self.relative else None
        """``(st_dev, st_ino)`` of the cwd before anything is resolved (the
        SPEC's "before the realpath is taken"), for a relative path; ``None``
        otherwise or when it cannot be looked up."""
        self.discard: Callable[[], None] | None = None


def _relative(path: str | bytes) -> bool:
    """Whether ``path`` is resolved against the cwd: not absolute, and not a
    Linux abstract address (leading NUL), which names no file."""
    raw = os.fsencode(path)
    return not raw.startswith(b"/") and not (_ABSTRACT and raw.startswith(b"\0"))


_ABSTRACT = sys.platform.startswith("linux") or sys.platform == "android"


def _cwd_id() -> tuple[int, int] | None:
    try:
        st = os.stat(".")
    except OSError:
        return None
    return st.st_dev, st.st_ino


_recording: ContextVar[_Recorder | None] = ContextVar("sockat_ex_bind_recording", default=None)


@contextlib.contextmanager
def recording(path: str | bytes) -> Iterator[_Recorder]:
    """Have the bind made inside the ``with`` block remember the socket file
    at ``path`` right after the bind goes through (:func:`bound`), before
    the listener is returned."""
    recorder = _Recorder(path)
    token = _recording.set(recorder)
    try:
        yield recorder
    finally:
        _recording.reset(token)


def bound() -> None:
    """Called by every route right after its bind went through (before the
    listen): when :func:`sockat.ex.bind` is recording, remember the socket file now,
    so a file put at the path after the bind returned cannot be remembered
    in its place. A failure to remember is raised, and the route closes the
    socket (a ``discard`` that could not remove the file must not look like
    one that can). For a relative path, a cwd changed since the call began is
    such a failure (:class:`CwdChanged`)."""
    recorder = _recording.get()
    if recorder is not None and recorder.discard is None:
        failure: OSError | None = None
        try:
            discard = remember(recorder.path)
        except OSError as e:
            failure = e
        # A relative path: the cwd is compared now, after the bind and the
        # realpath taken to remember, with the one before anything was
        # resolved (SPEC). Another thread that changed it in between would
        # have the bind made under one cwd and the file remembered under
        # another, and discard could then remove an unrelated file of the
        # same name. A change undone before this point is not seen. The
        # change is what is reported even when remembering failed, as it
        # does where the file is not under the new cwd.
        if recorder.relative and (recorder.cwd is None or _cwd_id() != recorder.cwd):
            raise CwdChanged(
                "the current directory changed while binding a path relative to it; the listener was closed"
            ) from failure
        if failure is not None:
            raise failure
        recorder.discard = discard


def remember(path: str | bytes) -> Callable[[], None]:
    """Remember the socket file just bound at ``path``: its realpath (so a
    later change of the cwd, or of a symlink on the way, does not matter) and
    its ``(st_dev, st_ino)``. Return ``discard``, which removes the file at
    that realpath only while it is still the same file, and does nothing when
    it is gone or has been replaced by another; it may be called any number
    of times. Errors other than the file being gone (``EACCES`` on the
    directory, ...) are raised by ``discard``.

    A Linux abstract address (leading NUL) names no file: ``discard`` does
    nothing.

    Between the check and the removal another process could still replace
    the file: POSIX has no call that removes a name only if it still refers
    to a given inode. The guarantee is against a file replaced before
    ``discard`` is called."""
    raw = os.fsencode(path)
    if (sys.platform.startswith("linux") or sys.platform == "android") and raw.startswith(b"\0"):
        return lambda: None
    real = os.fsencode(os.path.realpath(raw, strict=True))
    st = os.lstat(real)
    made = (st.st_dev, st.st_ino)

    def discard() -> None:
        try:
            now = os.lstat(real)
        except (FileNotFoundError, NotADirectoryError):
            # Already gone (a directory on the way gone or replaced by a
            # file counts too): success.
            return
        if (now.st_dev, now.st_ino) != made:
            return
        with contextlib.suppress(FileNotFoundError):
            os.unlink(real)

    return discard
