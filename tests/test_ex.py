"""``sockat.ex.bind``'s listener (that of :func:`sockat.bind`) and
``discard``, which removes the socket file the bind made and nothing else
(the SPEC's ``_ex``). ``discard`` does not close the listener. What the bind
went through (``losses``, ``route``, ``skipped``) is checked in
``test_ex_bind.py``."""

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
    bind_route_here,
    is_socket,
    listing,
    needs_long_bind,
    nest,
    parent,
    run_child,
)

import sockat
import sockat.ex
from sockat import _route, _socket_file
from sockat._sockets import PathSocket
from sockat._sys import DIRECT_MAX

LENGTHS = [pytest.param(60, id="short"), pytest.param(300, id="long", marks=needs_long_bind)]


# sockat.ex.bind's listener is what sockat.bind returns for the same length:
# a plain socket.socket for a path the socket module takes, a PathSocket
# past that, listening, reporting the path.
@pytest.mark.parametrize("total", LENGTHS)
def test_the_listener_is_binds(tmp: TmpDirs, total: int) -> None:
    p = nest(tmp.new(), total)
    listener, discard, *_ = sockat.ex.bind(p)
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
    listener, discard, *_ = sockat.ex.bind(p)
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
    listener, discard, *_ = sockat.ex.bind(p)
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
    listener, discard, *_ = sockat.ex.bind(p)
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
    listener, discard, *_ = sockat.ex.bind(p)

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
    listener, discard, *_ = sockat.ex.bind(addr)
    with listener:
        discard()
        with sockat.connect(addr) as client:
            accept_ping(listener, client)


# The socket file is remembered right after the bind goes through, inside the
# route and before the listen and the return (SPEC: remembering it after
# sockat.ex.bind got the listener back could take in a file put at the path in
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
    listener, discard, *_ = sockat.ex.bind(p)
    monkeypatch.setattr(socket.socket, "listen", real_listen)
    with listener:
        assert events == ["remember", "listen"], events
        discard()
    assert not os.path.exists(p)


# When the socket file cannot be remembered, sockat.ex.bind closes the listener and
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
        sockat.ex.bind(p)
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
    listener, discard, *_ = sockat.ex.bind(p)
    listener.close()
    top = base + b"/" + os.path.relpath(p, base).split(b"/")[0]
    aside = tmp.new() + b"/aside"
    os.rename(top, aside)
    with open(top, "wb"):
        pass
    discard()
    discard()
    assert os.path.isfile(top)


# sockat.ex.bind reads the path once and binds and remembers that one value: a
# path-like object may answer differently each time it is asked, and taking
# one answer to bind and another to remember would make discard remove an
# unrelated file. Stood in for by a path-like object that names an unrelated
# existing file first and the socket's path after; the unrelated file must
# survive, whichever answer was used (read once, the bind of the existing file
# fails).
class _Shifting:
    def __init__(self, first: bytes, then: bytes) -> None:
        self.answers = [first, then]

    def __fspath__(self) -> bytes:
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@pytest.mark.parametrize("total", LENGTHS)
def test_a_path_like_object_is_read_once(tmp: TmpDirs, total: int) -> None:
    p = nest(tmp.new(), total)
    victim = tmp.new() + b"/victim"
    with open(victim, "wb"):
        pass
    try:
        listener, discard, *_ = sockat.ex.bind(_Shifting(victim, p))
    except OSError:
        pass  # the one answer read names the existing file: EADDRINUSE
    else:
        with listener:
            discard()
    assert os.path.exists(victim)


# For a relative path, sockat.ex.bind compares the cwd before anything is
# resolved with the cwd after the bind (and the realpath taken to remember
# the file), and when they differ treats it as a socket file that cannot be
# remembered (SPEC): the listener is closed, the error raised (the general
# error, an OSError without errno, carrying the route that bound and the
# routes skipped), and no discard is handed back, so the file is left where
# the bind made it. Otherwise the bind would go to one cwd and the file be
# remembered under another, where discard could remove an unrelated file of
# the same name. The change is made from inside the call (in a child, as a
# chdir is process-wide), standing in for another thread, at three points:
#
# * before-bind: a short path, just before the standard's bind: the bind is
#   made under the new cwd (B), where the socket file is left; the file of
#   the same name in the old cwd (A) is left alone;
# * after-realpath: a long path, after the realpath of its directory was
#   taken and before the route's bind: the socket file is left under
#   whichever cwd the route bound it (macOS's symlink-file under B, the
#   routes through D under A, R pointing to the realpath taken before);
# * after-bind: right after the bind, before the file is remembered: the
#   socket file is left under A, and the file of the same name under B, the
#   one that would have been remembered, is left alone.
@pytest.mark.parametrize("mode", ["before-bind", "after-realpath", "after-bind"])
def test_a_cwd_changed_during_a_relative_bind_fails_it_and_leaves_the_files(tmp: TmpDirs, mode: str) -> None:
    a, b = tmp.new(), tmp.new()
    if mode == "after-realpath":
        if bind_route_here() is None:
            pytest.skip("no route of the default bind for a long path here")
        rel = nest(a, len(a) + 1 + 200)[len(a) + 1 :]
        os.makedirs(os.path.dirname(b + b"/" + rel))
    else:
        rel = b"server.sock"
    unrelated_in = {"before-bind": a, "after-bind": b}.get(mode)
    if unrelated_in is not None:
        with open(unrelated_in + b"/" + rel, "wb") as f:
            f.write(b"unrelated")
    out = run_child("ex_bind_cwd_changed", a, {"REL": rel, "OTHER": b, "MODE": mode.encode()})
    assert out["moved"] == "True", out
    assert (out["type"], out["errno"]) == ("CwdChanged", "None"), out
    assert out["route"] not in ("None", ""), out
    assert out["closed"] == "True", out
    if mode == "before-bind":
        assert (out["a"], out["b"]) == ("file:unrelated", "socket"), out
    elif mode == "after-bind":
        assert (out["a"], out["b"]) == ("socket", "file:unrelated"), out
    else:
        assert sorted([out["a"], out["b"]]) == ["nothing", "socket"], out
