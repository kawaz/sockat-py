"""The sockets from sockat handed to asyncio, the standard library's event
loop: the documented recipe, ``asyncio.start_unix_server(sock=sockat.bind(p))``
and ``asyncio.open_unix_connection(sock=sockat.connect_nonblocking(p))``."""

from __future__ import annotations

import asyncio

import pytest
from _support import TmpDirs, nest

import sockat
from sockat._sys import DIRECT_MAX


# A listener and a client from sockat, adopted by asyncio through the `sock=`
# parameters the standard provides for sockets made elsewhere, exchange data in
# both directions, at a short path (the standard's) and at long ones (well past
# sun_path and past the 253 bytes of the widened sockaddr on macOS).
# connect_nonblocking is the connect meant for an event loop: it does not wait
# on the loop's thread and returns a socket that is already nonblocking. The
# blocking `connect` works too (asyncio makes the socket nonblocking itself),
# so both are adopted.
@pytest.mark.parametrize("total", [60, DIRECT_MAX + 1, 400])
def test_sockets_from_sockat_are_adopted_by_asyncio(tmp: TmpDirs, total: int) -> None:
    path = nest(tmp.new(), total)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.readexactly(4)
        writer.write(b"pong" if data == b"ping" else b"????")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def main() -> list[bytes]:
        got: list[bytes] = []
        server = await asyncio.start_unix_server(handle, sock=sockat.bind(path))
        async with server:
            for sock in (sockat.connect_nonblocking(path), sockat.connect(path)):
                reader, writer = await asyncio.open_unix_connection(sock=sock)
                writer.write(b"ping")
                await writer.drain()
                got.append(await reader.readexactly(4))
                writer.close()
                await writer.wait_closed()
        return got

    assert asyncio.run(main()) == [b"pong", b"pong"]
