from __future__ import annotations

from collections.abc import Iterator

import pytest
from _support import TmpDirs


@pytest.fixture
def tmp() -> Iterator[TmpDirs]:
    """Randomly named directories directly under ``/tmp`` (``tmp.new()``),
    removed when the test ends."""
    dirs = TmpDirs()
    try:
        yield dirs
    finally:
        dirs.cleanup()
