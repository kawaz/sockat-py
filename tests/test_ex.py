"""``sockat.bind_ex``: the listener of :func:`sockat.bind` and ``discard``,
which removes the socket file the bind made and nothing else (the SPEC's
``_ex``). ``discard`` does not close the listener."""

from __future__ import annotations

import asyncio
import os
import socket
import sys

import pytest
from _support import (
    TmpDirs,
    accept_ping,
    is_socket,
    listing,
    needs_long_bind,
    nest,
    parent,
)

import sockat
from sockat._sockets import PathSocket
from sockat._sys import DIRECT_MAX

LENGTHS = [pytest.param(60, id="short"), pytest.param(300, id="long", marks=needs_long_bind)]


# bind_ex is public, next to bind, and its listener is what bind returns for
# the same length: a plain socket.socket for a path the socket module takes,
# a PathSocket past that, listening, reporting the path.
@pytest.mark.parametrize("total", LENGTHS)
def test_the_listener_is_binds(tmp: TmpDirs, total: int) -> None:
    assert "bind_ex" in sockat.__all__
    p = nest(tmp.new(), total)
    listener, discard = sockat.bind_ex(p)
    with listener:
        assert type(listener) is (socket.socket if total <= DIRECT_MAX else PathSocket)
        assert listener.getsockname() == os.fsdecode(p)
        with sockat.connect(p) as client:
            accept_ping(listener, client)
    discard()


# discard removes the socket file at the path, and only that: the directory
# holds nothing afterwards. It does not close the listener: a connection made
# before is still accepted and talks, and the listener is closed by its owner
# (here the `with`). Called again, with the file already gone, it does
# nothing.
@pytest.mark.parametrize("total", LENGTHS)
def test_discard_removes_the_file_and_leaves_the_listener_open(tmp: TmpDirs, total: int) -> None:
    p = nest(tmp.new(), total)
    listener, discard = sockat.bind_ex(p)
    with listener:
        client = sockat.connect(p)
        with client:
            discard()
            assert not os.path.lexists(p)
            assert listing(parent(p)) == []
            assert listener.fileno() != -1
            accept_ping(listener, client)
        discard()
        discard()
    assert not os.path.lexists(p)


# When the file at the path is no longer the one the bind made, discard
# leaves it alone: replaced by a regular file, or by another socket bound at
# the same path (after the first was removed by hand), it stays where it is.
@pytest.mark.parametrize("total", LENGTHS)
def test_discard_leaves_a_replacement_alone(tmp: TmpDirs, total: int) -> None:
    p = nest(tmp.new(), total)
    listener, discard = sockat.bind_ex(p)
    listener.close()
    os.unlink(p)
    with open(p, "wb") as f:
        f.write(b"theirs")
    discard()
    with open(p, "rb") as f:
        assert f.read() == b"theirs"
    os.unlink(p)
    with sockat.bind(p):
        discard()
        assert is_socket(p)


# The file is remembered by its realpath, taken right after the bind: a path
# through a symlinked directory whose link is pointed elsewhere afterwards
# (where a file of the same name sits) still has its own socket file removed,
# and the file the link now leads to is left alone.
@pytest.mark.parametrize("total", LENGTHS)
def test_discard_follows_the_realpath_taken_at_the_bind(tmp: TmpDirs, total: int) -> None:
    t = tmp.new()
    real = parent(nest(t + b"/real", total - len(t) - 6))
    other = t + b"/other"
    os.mkdir(other)
    os.symlink(t + b"/real", t + b"/link")
    p = t + b"/link" + real[len(t + b"/real") :] + b"/s.sock"
    listener, discard = sockat.bind_ex(p)
    listener.close()
    assert is_socket(real + b"/s.sock")
    os.makedirs(other + real[len(t + b"/real") :])
    with open(other + real[len(t + b"/real") :] + b"/s.sock", "wb"):
        pass
    os.unlink(t + b"/link")
    os.symlink(other, t + b"/link")
    discard()
    assert not os.path.lexists(real + b"/s.sock")
    assert os.path.lexists(other + real[len(t + b"/real") :] + b"/s.sock")


# The listener handed to asyncio as the standard's is (start_unix_server(sock=),
# nothing else given) serves as usual, and discard works with it: called while
# the server runs, it removes the file while the accepted stream goes on, and
# asyncio's own cleanup at the close (Python 3.13 and later) finds nothing to
# remove and does not fail; called after the server closed (which closed the
# listener: asyncio owns it then), it does nothing more, or removes the file
# asyncio left (before 3.13). Either way the file is gone at the end.
@pytest.mark.parametrize("total", LENGTHS)
@pytest.mark.parametrize("when", ["while serving", "after"])
def test_discard_works_with_asyncio(tmp: TmpDirs, total: int, when: str) -> None:
    p = nest(tmp.new(), total)
    listener, discard = sockat.bind_ex(p)

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.readexactly(4))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def main() -> None:
        server = await asyncio.start_unix_server(echo, sock=listener)
        async with server:
            reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(p))
            if when == "while serving":
                discard()
                assert not os.path.lexists(p)
            writer.write(b"ping")
            await writer.drain()
            assert await reader.readexactly(4) == b"ping"
            writer.close()
            await writer.wait_closed()

    asyncio.run(main())
    assert listener.fileno() == -1
    discard()
    assert not os.path.lexists(p)


# A Linux abstract address names no file: discard does nothing (and does not
# fail).
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the abstract namespace is Linux's")
def test_discard_of_an_abstract_address_does_nothing() -> None:
    addr = b"\0sockat-test-ex-" + os.urandom(8).hex().encode()
    listener, discard = sockat.bind_ex(addr)
    with listener:
        discard()
        with sockat.connect(addr) as client:
            accept_ping(listener, client)
