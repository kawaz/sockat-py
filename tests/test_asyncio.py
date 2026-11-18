"""The sockets from sockat handed to asyncio, the standard library's event
loop: the documented recipe, ``asyncio.start_unix_server(sock=sockat.bind(p))``
and ``asyncio.open_unix_connection(sock=sockat.connect_nonblocking(p))``, with
nothing else given, behaves at a long path as the same recipe with the
standard's sockets behaves at a short one. Every route is also put through
asyncio by :func:`_support.check_asyncio` (from ``check_route``)."""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Callable

import pytest
from _support import (
    TmpDirs,
    assert_cleaned_up_as_asyncio_does,
    needs_long_bind,
    nest,
    run_child,
    serve_asyncio,
)

import sockat
from sockat._route import Request
from sockat._sys import DIRECT_MAX


def public_connect(path: bytes) -> Callable[[Request], socket.socket]:
    """``sockat.connect_nonblocking`` / ``sockat.connect`` of ``path``, for
    the nonblocking / blocking request."""

    def connect(req: Request) -> socket.socket:
        return sockat.connect_nonblocking(path) if req.nonblocking else sockat.connect(path)

    return connect


# A listener and a client from sockat, adopted by asyncio through the `sock=`
# parameters the standard provides for sockets made elsewhere, with nothing
# else given (cleanup_socket is left at its default, on from Python 3.13),
# exchange data in both directions, at a short path (the standard's) and at
# long ones (past sun_path, and past the 253 bytes of the widened sockaddr on
# macOS). connect_nonblocking is the connect meant for an event loop: it does
# not wait on the loop's thread and returns a socket that is already
# nonblocking. The blocking `connect` works too (asyncio makes the socket
# nonblocking itself), so both are adopted.
#
# What asyncio reads back is the path given, as for the standard's sockets at
# a short path: the server's socket and the transport of each accepted stream
# report it as their address (`sockname`), the client's transport as its peer
# (`peername`). When the server closes, the socket file at the path is removed
# from Python 3.13 (asyncio stats the listener's address at the start and
# removes the file there at the close if it is the same inode) and left
# before, as for the standard's.
#
# The listener is bound before the event loop exists (asyncio.run after the
# bind: the usual way of making a server's socket) and, separately, on the
# loop's thread with the loop running. The first is the one that failed on
# Linux when the kernel's spelling was handed to asyncio: a bind through
# /proc leaves /proc/self/fd/<n>/... with a directory fd it closes again, the
# loop made afterwards takes the number <n> for its own fds, and the stat at
# the start of the server failed with NotADirectoryError. Bound in the loop,
# the number stays free and the same stat only finds nothing, which asyncio
# ignores, so that alone did not show it.
@pytest.mark.parametrize("bound", ["before the loop", "in the loop"])
@pytest.mark.parametrize("total", [60, DIRECT_MAX + 1, 400])
@needs_long_bind
def test_sockets_from_sockat_are_used_by_asyncio_as_the_standards_are(tmp: TmpDirs, total: int, bound: str) -> None:
    path = nest(tmp.new(), total)
    what = f"{total} bytes, bound {bound}"
    if bound == "before the loop":
        listener = sockat.bind(path)
        asyncio.run(serve_asyncio(what, listener, public_connect(path), os.fsdecode(path), served=True))
    else:

        async def main() -> None:
            await serve_asyncio(what, sockat.bind(path), public_connect(path), os.fsdecode(path), served=True)

        asyncio.run(main())
    assert_cleaned_up_as_asyncio_does(what, path)


# The close of a server must remove the socket file at the path and nothing
# else, even where the kernel's spelling of the listener's address would name
# another file if resolved against the process cwd: the central route's
# spelling is `R/<name>` (or behind /proc/self/fd/<n>/), and a file of the
# socket's name sits in the cwd. Had asyncio been handed the spelling, a
# spelling without R (the file name alone) would have named that file, whose
# inode asyncio would record at the start and remove at the close. The cwd is
# set for the test, so this runs in a child process
# (_children.asyncio_namesakes).
@needs_long_bind
def test_asyncio_removes_the_socket_file_not_a_namesake_from_the_cwd(tmp: TmpDirs) -> None:
    t = tmp.new()
    p = nest(tmp.new(), 600, b"s.sock")
    assert run_child("asyncio_namesakes", t, {"PATH": p}) == {}
