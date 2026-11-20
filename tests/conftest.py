from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

import pytest
from _support import TmpDirs

from sockat import _place


@pytest.fixture
def tmp() -> Iterator[TmpDirs]:
    """Randomly named directories directly under ``/tmp`` (``tmp.new()``),
    removed when the test ends."""
    dirs = TmpDirs()
    try:
        yield dirs
    finally:
        dirs.cleanup()


@pytest.fixture(autouse=True, scope="session")
def _remove_own_d() -> Iterator[None]:
    """The tests of the public calls make this process's D in the real B
    (``/tmp/sockat-<uid>`` or another default place). Remove it, and the E
    left in it, at the end of the session, rather than leave it to the
    system's cleaning of temporary files."""
    yield
    for b in _place.state().bases():
        d = b._d
        if d is None or d.closed:
            continue
        with contextlib.suppress(OSError), os.scandir(d.fd) as entries:
            names = [e.name for e in entries]
        for name in names:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=d.fd)
        with contextlib.suppress(OSError):
            os.rmdir(d.name, dir_fd=b.fd)
    _place.state().abandon()
