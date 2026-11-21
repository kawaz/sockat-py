"""The code the manual shows for writing instead of the patterns not to use
("使ってはいけない書き方" / "Patterns not to use"), read out of
``docs/MANUAL-ja.md`` (the original) and ``docs/MANUAL.md`` and run as it is
written (hub SPEC: the recommended code is run as a test). The code lives in
the documents only, so what is run is what a reader copies.

Each block runs in a child process of its own (it may change the cwd), with
``path`` set to the socket's path: a path the block makes absolute itself
(``os.path.join(os.getcwd(), path)``) is given relative to the child's cwd; any other
is absolute and long (past every ``sun_path``). A
file of the socket's name unrelated to it sits in the child's cwd. Every
block must run through, leave no socket file at the path, and leave that
unrelated file alone."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from _support import PRIVATE_PLACE_CODE, SRC_DIR, TESTS_DIR, TmpDirs, nest

DOCS = TESTS_DIR.parent / "docs"

SECTIONS = {
    "MANUAL-ja.md": ("使ってはいけない書き方",),
    "MANUAL.md": ("Patterns not to use",),
}
"""The sections whose code is run, per document (the same sections, the
Japanese one the original)."""

NAME = "server.sock"

UNRELATED = b"unrelated, not to be removed"


@dataclass(frozen=True)
class Block:
    doc: str
    section: str
    heading: str
    code: str

    @property
    def id(self) -> str:
        return f"{self.doc}:{self.section}:{self.heading}"


def blocks(doc: str) -> list[Block]:
    """The ```python blocks of the sections :data:`SECTIONS` names in
    ``doc``, each with the ``###`` heading it sits under. Blocks of other
    languages are passed over."""
    found: list[Block] = []
    section = heading = ""
    fence: str | None = None
    code: list[str] = []
    for line in (DOCS / doc).read_text(encoding="utf-8").splitlines():
        if fence is not None:
            if line.startswith("```"):
                if fence == "python" and section in SECTIONS[doc]:
                    found.append(Block(doc, section, heading, "\n".join(code) + "\n"))
                fence = None
            else:
                code.append(line)
        elif line.startswith("```"):
            fence, code = line[3:].strip(), []
        elif line.startswith("## "):
            section, heading = line[3:].strip(), ""
        elif line.startswith("### "):
            heading = line[4:].strip()
    return found


ALL = [b for doc in SECTIONS for b in blocks(doc)]


# Every section named carries code in both documents, and the English one
# carries the same code as the Japanese original, block for block, so the
# two cannot drift apart: what a reader of either copies is what is run.
def test_the_documents_carry_the_same_code() -> None:
    ja, en = blocks("MANUAL-ja.md"), blocks("MANUAL.md")
    for doc in SECTIONS:
        assert {b.section for b in blocks(doc)} == set(SECTIONS[doc]), doc
    assert len(ja) == len(en) == 8, (len(ja), len(en))
    assert [b.code for b in ja] == [b.code for b in en]


IDS = [f"{doc}#{n}" for doc in SECTIONS for n in range(1, len(blocks(doc)) + 1)]
"""Test ids in ASCII (pytest escapes others): the document and the block's
number in it; the block's sections and heading are in a failure's
message."""


@pytest.mark.parametrize("block", ALL, ids=IDS)
def test_the_code_runs(tmp: TmpDirs, block: Block) -> None:
    cwd = tmp.new()
    if "os.getcwd()" in block.code:
        given: bytes = NAME.encode()
        target = cwd + b"/" + given
    else:
        target = nest(tmp.new(), 300, NAME.encode())
        given = target
        with open(cwd + b"/" + NAME.encode(), "wb") as f:
            f.write(UNRELATED)
    script = PRIVATE_PLACE_CODE + f"path = {os.fsdecode(given)!r}\n" + block.code
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC_DIR), env.get("PYTHONPATH")]))
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=60
    )
    assert out.returncode == 0, f"{block.id}\n--- code\n{block.code}--- stderr\n{out.stderr}"
    assert not os.path.lexists(target), f"{block.id}: the socket file is left at the path"
    if given == target:
        assert (Path(os.fsdecode(cwd)) / NAME).read_bytes() == UNRELATED, f"{block.id}: the unrelated file was touched"
