"""Pure path arithmetic on the bytes of a socket path: splitting it into its
directory and file name, and spelling an absolute path relative to a working
directory. Nothing here touches the file system."""

from __future__ import annotations


def split(path: bytes) -> tuple[bytes, bytes]:
    """Split a socket path into the directory that holds the socket and the
    socket's file name, by the bytes after the last ``/``.

    The split is done on the bytes rather than through a normalising path
    library, which would drop a trailing ``/`` and ``.``: ``a/b/`` and
    ``a/b/.`` would both come out as file name ``b``, while the kernel would
    resolve them as the directory ``a/b`` itself. Such paths do not name a
    socket file, so they are refused rather than silently retargeted.

    ============== =========== =========
    path           directory   file name
    ============== =========== =========
    ``/d/e/s.sock`` ``/d/e``    ``s.sock``
    ``/s.sock``    ``/``       ``s.sock``
    ``s.sock``     ``.``       ``s.sock``
    ``d//s.sock``  ``d/``      ``s.sock``
    ``d/``, ``d/.``, ``d/..``, ``..`` (ValueError)
    ============== =========== =========

    Raises:
        ValueError: the last segment is empty, ``.`` or ``..``.
    """
    i = path.rfind(b"/")
    if i == 0:
        directory, name = b"/", path[1:]
    elif i > 0:
        directory, name = path[:i], path[i + 1 :]
    else:
        directory, name = b".", path
    if name in (b"", b".", b".."):
        raise ValueError("socket path must end with a file name (not '/', '.' or '..')")
    return directory, name


def components(path: bytes) -> list[bytes]:
    """The components of an absolute path after the root, as the kernel walks
    them: empty segments (doubled slashes) and ``.`` are dropped, ``..`` is
    kept as it is (it is not interpreted)."""
    return [c for c in path.split(b"/") if c not in (b"", b".")]


def below(target: bytes, prefix: bytes) -> bytes | None:
    """The part of ``target`` below the directory ``prefix``, when ``target``
    starts with ``prefix`` component by component (``/a/b`` is a prefix of
    ``/a/b/c`` but not of ``/a/bc``).

    The result only descends from ``prefix`` along ``target``'s own
    components, so it resolves through the same symlinks ``target`` does.
    ``None`` when either is not absolute or ``prefix`` is not a proper prefix
    (including ``target == prefix``, which leaves no file name).
    """
    if not target.startswith(b"/") or not prefix.startswith(b"/"):
        return None
    t = components(target)
    p = components(prefix)
    if len(t) <= len(p) or t[: len(p)] != p:
        return None
    return b"/".join(t[len(p) :])


def relative(target: bytes, cwd: bytes) -> bytes | None:
    """Spell the absolute path ``target`` relative to the absolute directory
    ``cwd``: climb with ``..`` out of the part of ``cwd`` that ``target`` does
    not share, then descend along the rest of ``target``.

    This is component arithmetic and is only meaningful when both inputs are
    real paths (no symlinks, ``.`` or ``..``): ``cwd`` as ``getcwd(3)`` reports
    it and ``target`` with its directory passed through ``realpath(3)``. Then
    each ``..`` climbs out of a real directory, which is what the kernel does.
    The caller still confirms that the result names the same directory before
    using it.

    ``None`` when either path is not absolute, or when ``target`` is ``cwd``
    itself or one of its ancestors (there would be no file name left).
    """
    if not target.startswith(b"/") or not cwd.startswith(b"/"):
        return None
    t = components(target)
    c = components(cwd)
    common = 0
    for a, b in zip(t, c, strict=False):
        if a != b:
            break
        common += 1
    if common >= len(t):
        return None
    return b"/".join([b".."] * (len(c) - common) + t[common:])
