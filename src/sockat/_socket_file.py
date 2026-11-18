"""The socket file a bind made, remembered so that it can be removed later
without removing anything else (``bind_ex``, the SPEC's ``_ex``)."""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable


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
    the file (there is no unlink-if-same-inode call); the guarantee is
    against a file replaced before ``discard`` is called."""
    raw = os.fsencode(path)
    if (sys.platform.startswith("linux") or sys.platform == "android") and raw.startswith(b"\0"):
        return lambda: None
    real = os.fsencode(os.path.realpath(raw, strict=True))
    st = os.lstat(real)
    made = (st.st_dev, st.st_ino)

    def discard() -> None:
        try:
            now = os.lstat(real)
        except FileNotFoundError:
            return
        if (now.st_dev, now.st_ino) != made:
            return
        with contextlib.suppress(FileNotFoundError):
            os.unlink(real)

    return discard
