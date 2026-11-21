from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator

import pytest
from _support import PRIVATE_PLACE_ENV, STATES, TmpDirs, private_config

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


@pytest.fixture(autouse=True)
def _close_test_states() -> Iterator[None]:
    """Close the B and D fds of the states a test made (``env_at``): they
    are held for the life of a state, and a test's state ends with the test.
    Left open, they pile up over the session past a small fd limit (OpenBSD's
    default)."""
    yield
    while STATES:
        STATES.pop().abandon()


@pytest.fixture(autouse=True, scope="session")
def _private_place() -> Iterator[None]:
    """The places the public calls read from the environment are made this
    session's own (a directory of its own, removed at the end), in this
    process and in the child processes the tests start
    (:data:`PRIVATE_PLACE_ENV`): the tests never make, use or remove
    anything in the shared ``/tmp/sockat-<uid>``, which other processes (the
    other languages' tests run at the same time) use."""
    place = tempfile.mkdtemp(prefix="spt-place-", dir="/tmp")
    # Others can search it, as /tmp: a spelling through it can be followed by
    # another uid (usable_cross_uid), as through the shared place.
    os.chmod(place, 0o755)
    real_from_env = _place.Config.from_env
    _place.Config.from_env = classmethod(lambda cls: private_config(os.fsencode(place)))  # type: ignore[method-assign,assignment]
    os.environ[PRIVATE_PLACE_ENV] = place
    try:
        yield
    finally:
        _place.Config.from_env = real_from_env  # type: ignore[method-assign]
        os.environ.pop(PRIVATE_PLACE_ENV, None)
        _place.state().abandon()
        shutil.rmtree(place, ignore_errors=True)
