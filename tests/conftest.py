from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator

import pytest
from _support import PRIVATE_PLACE_ENV, TmpDirs, private_config

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
def _private_place() -> Iterator[None]:
    """The places the public calls read from the environment are made this
    session's own (a directory of its own, removed at the end), in this
    process and in the child processes the tests start
    (:data:`PRIVATE_PLACE_ENV`): the tests never make, use or remove
    anything in the shared ``/tmp/sockat-<uid>``, which other processes (the
    other languages' tests run at the same time) use."""
    place = tempfile.mkdtemp(prefix="spt-place-", dir="/tmp")
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
