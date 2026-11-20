"""The sockets from sockat handed to asyncio, the standard library's event
loop: ``asyncio.start_unix_server(sock=sockat.bind(p))`` and
``asyncio.open_unix_connection(sock=sockat.connect_nonblocking(p))``, with
nothing else given, behave at a long path as the standard's at a short one
(hub SPEC "カーネルが返すアドレス": the representative library above works
without an argument added)."""

from __future__ import annotations

import asyncio
import os
import socket
import sys

import pytest
from _support import TmpDirs, is_socket, long_path, nest

import sockat
from sockat._sys import DIRECT_MAX

ASYNCIO_CLEANS_UP = sys.version_info >= (3, 13)
"""Whether ``start_unix_server(sock=)`` removes the listener's socket file
when the server closes (``cleanup_socket``, Python 3.13): it reads the
listener's ``getsockname()`` at the start and at the close."""


async def serve(path: str, listener: socket.socket | None) -> None:
    """Serve ``listener`` (or one bound now, in the loop) with asyncio and
    connect to it both ways."""
    names: list[object] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        names.append(writer.get_extra_info("sockname"))
        data = await reader.readexactly(4)
        writer.write(b"pong" if data == b"ping" else b"????")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, sock=listener or sockat.bind(path))
    async with server:
        assert [s.getsockname() for s in server.sockets] == [path]
        for connect in (sockat.connect_nonblocking, sockat.connect):
            sock = connect(path)
            sock.setblocking(False)
            reader, writer = await asyncio.open_unix_connection(sock=sock)
            assert writer.get_extra_info("peername") == path
            writer.write(b"ping")
            await writer.drain()
            assert await reader.readexactly(4) == b"pong"
            writer.close()
            await writer.wait_closed()
    assert names == [path, path]


# Bound before the loop exists (the usual way, and the one where the loop's
# own fds come after the bind) and bound inside the running loop: the server
# starts, serves and closes without an error, every address asyncio reads is
# the path, and the socket file is left where asyncio leaves the standard's
# (removed from 3.13, kept before), for a long path as for a short one.
@pytest.mark.parametrize("long", [False, True])
@pytest.mark.parametrize("bind_in_loop", [False, True])
def test_asyncio_serves_and_cleans_up_as_for_the_standard(tmp: TmpDirs, long: bool, bind_in_loop: bool) -> None:
    p = long_path(tmp.new()) if long else nest(tmp.new(), DIRECT_MAX - 10)
    path = os.fsdecode(p)
    asyncio.run(serve(path, None if bind_in_loop else sockat.bind(path)))
    if ASYNCIO_CLEANS_UP:
        assert not os.path.lexists(p)
    else:
        assert is_socket(p)
        os.unlink(p)
