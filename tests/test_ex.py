"""``sockat.bind_ex``: the listener of :func:`sockat.bind` and ``discard``,
which removes the socket file the bind made and nothing else (the SPEC's
``_ex``). ``discard`` does not close the listener."""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import sys
from collections.abc import Callable

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
from sockat import _route, _socket_file
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


# The socket file is remembered right after the bind goes through, inside the
# route and before the listen and the return (SPEC: remembering it after
# bind_ex got the listener back could take in a file put at the path in
# between). Checked by the order of the calls: remembering comes before the
# listen, and what it finds at the path then is the socket the bind made.
@pytest.mark.parametrize("total", LENGTHS)
def test_the_file_is_remembered_before_the_listener_is_returned(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, total: int
) -> None:
    p = nest(tmp.new(), total)
    events: list[str] = []
    real_remember = _socket_file.remember
    real_listen = socket.socket.listen

    def remember(path: str | bytes) -> Callable[[], None]:
        events.append("remember" if stat.S_ISSOCK(os.lstat(p).st_mode) else "remember-no-socket")
        return real_remember(path)

    def listen(self: socket.socket, *args: int) -> None:
        events.append("listen")
        real_listen(self, *args)

    monkeypatch.setattr(_socket_file, "remember", remember)
    monkeypatch.setattr(socket.socket, "listen", listen)
    listener, discard = sockat.bind_ex(p)
    monkeypatch.setattr(socket.socket, "listen", real_listen)
    with listener:
        assert events == ["remember", "listen"], events
        discard()
    assert not os.path.exists(p)


# When the socket file cannot be remembered, bind_ex closes the listener and
# raises, leaving the file where it is: a discard that could not remove the
# file must not be handed back as if it could.
@pytest.mark.parametrize("total", LENGTHS)
def test_a_file_that_cannot_be_remembered_fails_the_bind_and_closes_the_listener(
    tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch, total: int
) -> None:
    p = nest(tmp.new(), total)
    made: list[socket.socket] = []
    real_new_socket = _route.new_socket

    def new_socket(req: _route.Request, *, standard: bool = False) -> socket.socket:
        sock = real_new_socket(req, standard=standard)
        made.append(sock)
        return sock

    def remember(path: str | bytes) -> Callable[[], None]:
        raise PermissionError(13, "stand-in")

    monkeypatch.setattr(_route, "new_socket", new_socket)
    monkeypatch.setattr(_socket_file, "remember", remember)
    with pytest.raises(PermissionError):
        sockat.bind_ex(p)
    assert made
    assert all(s.fileno() == -1 for s in made)
    assert stat.S_ISSOCK(os.lstat(p).st_mode)


# discard succeeds when the file is already gone, including when a directory
# on its way was replaced by a file (lstat answers ENOTDIR): only a removal
# that was tried and failed is an error (SPEC).
@needs_long_bind
def test_discard_succeeds_when_a_directory_on_the_way_is_gone(tmp: TmpDirs) -> None:
    base = tmp.new()
    p = nest(base, 300)
    listener, discard = sockat.bind_ex(p)
    listener.close()
    top = base + b"/" + os.path.relpath(p, base).split(b"/")[0]
    aside = tmp.new() + b"/aside"
    os.rename(top, aside)
    with open(top, "wb"):
        pass
    discard()
    discard()
    assert os.path.isfile(top)
