"""The absolute candidates of a path for bind and resolve (hub SPEC
"相対パスを絶対パスにする", SPEC-routes "絶対パスの候補の作り方"): spellings
of the same place that do not depend on the cwd. A candidate that cannot be
made is no error: it is just not there."""

from __future__ import annotations

import os
import stat

from . import _path
from ._sys import DIRECT_MAX


def _ident(path: bytes) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return st.st_dev, st.st_ino


def _join(a: bytes, b: bytes) -> bytes:
    return a + b if a.endswith(b"/") else a + b"/" + b


def real_candidate(path: bytes) -> bytes | None:
    """The realpath of the socket's directory (symlinks followed, as the
    system looks it up) and the file name; ``None`` when the directory is
    not one or cannot be resolved. ``path`` ends with a file name.

    The directory is resolved with ``os.path.realpath`` as it is given, not
    made absolute with ``os.path.abspath`` first (that would fold
    ``a/sym/..`` as text, and realpath would look up another directory)."""
    directory, name = _path.split(path)
    try:
        if not stat.S_ISDIR(os.stat(directory).st_mode):
            return None
        real = os.fsencode(os.path.realpath(directory, strict=True))
    except (OSError, ValueError):
        return None
    return _join(real, name)


def pwd_candidate(path: bytes) -> bytes | None:
    """``$PWD`` and the relative ``path`` joined and folded as text, when
    ``$PWD`` is absolute and is the cwd (an inherited variable may not have
    been set again after a change of directory), and the folded directory is
    the one ``path``'s directory is from the cwd (``a/sym/..`` folds to
    another directory than the kernel's)."""
    pwd = os.environb.get(b"PWD")
    if not pwd or not pwd.startswith(b"/"):
        return None
    here = _ident(b".")
    if here is None or _ident(pwd) != here:
        return None
    folded = _path.fold(_join(pwd, path))
    directory, _ = _path.split(path)
    folded_dir, _ = _path.split(folded)
    meant = _ident(directory)
    if meant is None or _ident(folded_dir) != meant:
        return None
    return folded


def fitting(path: bytes) -> bytes | None:
    """The first absolute candidate of the relative ``path`` that the
    ``socket`` module takes (the real one first; the ``$PWD`` one is made
    only when that one does not fit), or ``None``."""
    real = real_candidate(path)
    if real is not None and len(real) <= DIRECT_MAX:
        return real
    pwd = pwd_candidate(path)
    if pwd is not None and len(pwd) <= DIRECT_MAX:
        return pwd
    return None
