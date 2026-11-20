"""What a bind's spelling is good for later (the hub SPEC's ``Spelling``),
and the diagnostics of the ``_ex`` calls and resolve (``route``, ``base``,
``skipped``)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple


class Until(StrEnum):
    """How long a :class:`Linked` spelling can be followed."""

    PROCESS = "Process"
    """Until the process ends (procB, ``/proc/<pid>/fd/<fd>/...``)."""
    CLEANUP = "Cleanup"
    """E until ``cleanup()``, D until the system's cleaning of temporary
    files after the process has ended (B by its real path)."""


class Spelling:
    """What the spelling a bind handed to the kernel (and the kernel reports
    back through ``getsockname`` / ``getpeername``) is good for later: one of
    :class:`Original`, :class:`CwdRelative`, :class:`Linked` and
    :class:`Dangling`. Branch on this rather than on the route's name (more
    kinds may come: read one you do not know as not usable)."""

    __slots__ = ()


@dataclass(frozen=True)
class Original(Spelling):
    """The absolute path given (the standard)."""


@dataclass(frozen=True)
class CwdRelative(Spelling):
    """The relative path given (the standard): once the cwd changes it names
    another place. A danger of the standard's own, reported all the same."""


@dataclass(frozen=True)
class Linked(Spelling):
    """A spelling through B, D and E that names the same place as the path
    given."""

    removable: bool
    """Whether unlinking by the spelling removes the socket file (link-dir:
    ``True``, through E in the middle; link-file: ``False``, the last symlink
    is not followed and E goes instead: use ``remove()``)."""
    until: Until
    """How long it can be followed."""
    usable_cross_uid: bool
    """Whether a peer of another uid can follow it."""
    usable_with_private_tmp: bool
    """Whether a peer can follow it while either side has a private ``/tmp``
    (systemd's ``PrivateTmp`` and the like): it does not go through ``/tmp``
    or ``/var/tmp``. A peer in another mount namespace altogether (a
    container) is not promised."""


@dataclass(frozen=True)
class Dangling(Spelling):
    """It holds an E already removed (thread-cwd, at): it names nothing."""


class Skipped(NamedTuple):
    """A route tried and found unusable. For diagnosis only: neither the
    names nor the reasons are promised (the routes may be added to or
    changed)."""

    name: str
    """The route's name in the sockat SPEC (``"link-dir"``, ``"thread-cwd"``,
    ``"proc-direct"``, ...)."""
    reason: str
    """Why it could not be used, a short phrase (``"spelling too long"``,
    ``"/proc unusable"``, ...)."""


class Trail:
    """The routes tried and found unusable, in order, one per name (the
    reason the last one found), and the route being tried now: the one whose
    bind / connect failed when the call raises."""

    def __init__(self) -> None:
        self.skipped: list[Skipped] = []
        self.trying: str | None = None

    def skip(self, name: str, why: str) -> None:
        """Note the route ``name`` as unusable for ``why``."""
        self.trying = None
        for i, s in enumerate(self.skipped):
            if s.name == name:
                self.skipped[i] = Skipped(name, why)
                return
        self.skipped.append(Skipped(name, why))

    def attempt(self, name: str) -> None:
        """Note that the route ``name`` is being tried (its bind / connect
        runs, or a resolve returns its spelling). A route noted as skipped
        under the same name (link-dir by procB before link-dir by B's real
        path) is no longer: the name is the one taken, or the one failed."""
        self.trying = name
        self.skipped = [s for s in self.skipped if s.name != name]

    def tell(self, e: BaseException) -> None:
        """Add ``skipped`` and ``route`` to the error ``e``.

        Design rationale: the error is the one the plain call raises, with
        attributes added rather than wrapped or subclassed, so that its type
        (FileNotFoundError, ...) and errno stay what callers test."""
        setattr(e, "skipped", tuple(self.skipped))  # noqa: B010
        setattr(e, "route", self.trying)  # noqa: B010
