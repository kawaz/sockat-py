"""The code the documents show, read out of them and run as it is written
(hub SPEC: the recommended code is run as a test). The code lives in the
documents only, so what is run is what a reader copies. The Japanese
documents are the originals; the English ones carry the same code, block for
block.

* The user manual's pages (``docs/manual/*.md``): every block, with ``path``
  a long path (past every ``sun_path``), ``base`` a directory for
  ``Options.base`` and ``handle`` an asyncio connection handler. A block
  that binds nothing finds a listener (of the block's socket type) already
  at ``path``; a block with ``await`` runs in an event loop. Every block
  must run through. A SEQPACKET block runs only where the ``socket`` module
  can make a SEQPACKET socket.
* The reference's code for writing instead of the patterns not to use
  ("使ってはいけない書き方" / "Patterns not to use"): ``path`` is relative to
  the child's cwd for a block that changes the cwd, otherwise absolute and
  long, with a file of the socket's name unrelated to it in the child's cwd.
  Every block must run through, leave no socket file at the path, and leave
  that unrelated file alone.

Each block runs in a child process of its own (it may change the cwd)."""

from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest
from _support import PRIVATE_PLACE_CODE, SRC_DIR, TESTS_DIR, TmpDirs, nest

DOCS = TESTS_DIR.parent / "docs"

PAGES = ("bind", "connect", "datagram", "resolve")
"""The user manual's pages with code (``docs/manual/<page>-ja.md`` and
``<page>.md``). The SEQPACKET page only points to the resolve page."""

PATTERNS = {
    "REFERENCE-ja.md": "使ってはいけない書き方",
    "REFERENCE.md": "Patterns not to use",
}
"""The reference's section whose code is run, per document."""

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


def blocks(doc: str, section: str | None = None) -> list[Block]:
    """The ```python blocks of ``doc`` (a path below ``docs/``), each with
    the ``##`` and ``###`` headings it sits under; only those under the
    ``##`` heading ``section`` when it is given. Blocks of other languages
    are passed over."""
    found: list[Block] = []
    current = heading = ""
    fence: str | None = None
    code: list[str] = []
    for line in (DOCS / doc).read_text(encoding="utf-8").splitlines():
        if fence is not None:
            if line.startswith("```"):
                if fence == "python" and section in (None, current):
                    found.append(Block(doc, current, heading, "\n".join(code) + "\n"))
                fence = None
            else:
                code.append(line)
        elif line.startswith("```"):
            fence, code = line[3:].strip(), []
        elif line.startswith("## "):
            current, heading = line[3:].strip(), ""
        elif line.startswith("### "):
            heading = line[4:].strip()
    return found


MANUAL = [b for page in PAGES for doc in (f"manual/{page}-ja.md", f"manual/{page}.md") for b in blocks(doc)]
REFERENCE = [b for doc, section in PATTERNS.items() for b in blocks(doc, section)]


def _ids(found: list[Block]) -> list[str]:
    """Test ids in ASCII (pytest escapes others): the document and the
    block's number in it; the block's headings are in a failure's
    message."""
    out: list[str] = []
    seen: dict[str, int] = {}
    for b in found:
        seen[b.doc] = seen.get(b.doc, 0) + 1
        out.append(f"{b.doc}#{seen[b.doc]}")
    return out


# The English pages carry the same code as the Japanese originals, block for
# block, so the two cannot drift apart: what a reader of either copies is
# what is run. Every page has code, and so has the reference's section.
def test_the_documents_carry_the_same_code() -> None:
    for page in PAGES:
        ja, en = blocks(f"manual/{page}-ja.md"), blocks(f"manual/{page}.md")
        assert ja, page
        assert [b.code for b in ja] == [b.code for b in en], page
    ja_ref, en_ref = (blocks(doc, section) for doc, section in PATTERNS.items())
    assert len(ja_ref) == len(en_ref) == 8, (len(ja_ref), len(en_ref))
    assert [b.code for b in ja_ref] == [b.code for b in en_ref]


def _run(block: Block, script: str, cwd: bytes) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC_DIR), env.get("PYTHONPATH")]))
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=60
    )
    assert out.returncode == 0, f"{block.id}\n--- code\n{block.code}--- stderr\n{out.stderr}"


def _seqpacket_refused() -> str | None:
    """Why this system's ``socket`` module cannot make a SEQPACKET socket
    (macOS: ``EPROTONOSUPPORT``), or ``None`` when it can."""
    try:
        socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET).close()
    except OSError as e:
        return f"the socket module cannot make a SEQPACKET socket here ({errno.errorcode.get(e.errno or 0, e.errno)})"
    return None


SEQPACKET_REFUSED = _seqpacket_refused()

LISTENER_CODE = """
import os as _os, socket as _socket, tempfile as _tempfile
_short = _os.path.join(_tempfile.mkdtemp(prefix="spt-d-", dir="/tmp"), "l.sock")
_listener = _socket.socket(_socket.AF_UNIX, _socket.SOCK_SEQPACKET if SEQPACKET else _socket.SOCK_STREAM)
_listener.bind(_short)
_listener.listen(16)
_os.rename(_short, path)
"""
"""A listener at ``path`` (bound under a short name by the standard and
renamed), for a block that only connects."""


@pytest.mark.parametrize("block", MANUAL, ids=_ids(MANUAL))
def test_the_manual_code_runs(tmp: TmpDirs, block: Block) -> None:
    seqpacket = "SOCK_SEQPACKET" in block.code
    if seqpacket and SEQPACKET_REFUSED is not None:
        pytest.skip(SEQPACKET_REFUSED)
    path = nest(tmp.new(), 300, NAME.encode())
    script = PRIVATE_PLACE_CODE + f"path = {os.fsdecode(path)!r}\nbase = {os.fsdecode(tmp.new() + b'/b')!r}\n"
    if "bind(" not in block.code:
        script += f"SEQPACKET = {seqpacket}\n" + LISTENER_CODE
    if "await " in block.code:
        script += (
            "import asyncio\n"
            "async def handle(reader, writer):\n    writer.close()\n"
            "async def _main():\n" + textwrap.indent(block.code, "    ") + "asyncio.run(_main())\n"
        )
    else:
        script += block.code
    _run(block, script, tmp.new())


@pytest.mark.parametrize("block", REFERENCE, ids=_ids(REFERENCE))
def test_the_reference_code_runs(tmp: TmpDirs, block: Block) -> None:
    cwd = tmp.new()
    if "os.chdir" in block.code:
        given: bytes = NAME.encode()
        target = cwd + b"/" + given
    else:
        target = nest(tmp.new(), 300, NAME.encode())
        given = target
        with open(cwd + b"/" + NAME.encode(), "wb") as f:
            f.write(UNRELATED)
    _run(block, PRIVATE_PLACE_CODE + f"path = {os.fsdecode(given)!r}\n" + block.code, cwd)
    assert not os.path.lexists(target), f"{block.id}: the socket file is left at the path"
    if given == target:
        assert (Path(os.fsdecode(cwd)) / NAME).read_bytes() == UNRELATED, f"{block.id}: the unrelated file was touched"
