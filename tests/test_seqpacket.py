"""SEQPACKET through the resolve calls (there are no SEQPACKET functions):
the shape the user manual shows (hub issue 2026-11-18-datagram-and-seqpacket,
the shape sockat-rs set): the socket made first, ``resolve_for_bind`` and a
``cleanup()`` when the bind or listen fails, ``socket_file()`` once they went
through; ``resolve_for_connect`` and a ``cleanup()`` right after the connect;
``remove()`` before the listener is closed. Run only where the ``socket``
module can make a SEQPACKET socket (not macOS)."""

from __future__ import annotations

import errno
import os
import socket

import pytest
from _support import TmpDirs, ds, is_socket, long_path, raises_errno, standard_errno

import sockat.ex


def _refused() -> str | None:
    try:
        socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET).close()
    except OSError as e:
        return f"the socket module cannot make a SEQPACKET socket here ({errno.errorcode.get(e.errno or 0, e.errno)})"
    return None


REFUSED = _refused()

pytestmark = pytest.mark.skipif(REFUSED is not None, reason=REFUSED or "")

SEQPACKET = socket.SOCK_SEQPACKET if REFUSED is None else 0


def listen_at(path: bytes, options: sockat.ex.Options) -> tuple[socket.socket, sockat.ex.SocketFile, str | bytes]:
    """A SEQPACKET listener at ``path`` as the manual binds it, its
    SocketFile and the spelling bound."""
    listener = socket.socket(socket.AF_UNIX, SEQPACKET)
    r = sockat.ex.resolve_for_bind(path, options=options)
    try:
        listener.bind(r.path)
        listener.listen()
    except OSError:
        r.cleanup()
        listener.close()
        raise
    return listener, r.socket_file(), r.path


def connect_to(path: bytes, options: sockat.ex.Options) -> socket.socket:
    """A SEQPACKET socket connected to ``path`` as the manual connects it
    (the spelling cleaned up right after the connect)."""
    client = socket.socket(socket.AF_UNIX, SEQPACKET)
    r = sockat.ex.resolve_for_connect(path, options=options)
    try:
        client.connect(r.path)
    except OSError:
        client.close()
        raise
    finally:
        r.cleanup()
    return client


@pytest.fixture
def options(tmp: TmpDirs) -> sockat.ex.Options:
    return sockat.ex.Options(base=tmp.new() + b"/b")


def e_left(o: sockat.ex.Options) -> list[bytes]:
    assert o.base is not None
    b = os.fsencode(o.base)
    return [e for d in ds(b) for e in os.listdir(b + b"/" + d)]


# A long path connects, both ways, after the connect's spelling was cleaned up
# right away; each message keeps its bounds (two sends are two receives). The
# addresses are spellings, not the path: the listener's (and the accepted
# socket's) getsockname and the client's getpeername are the spelling the
# listener bound (the client's own connect spelling is not reported).
def test_a_long_path_connects_with_messages_kept_apart(tmp: TmpDirs, options: sockat.ex.Options) -> None:
    p = long_path(tmp.new())
    listener, file, bound = listen_at(p, options)
    with listener:
        client = connect_to(p, options)
        server, _ = listener.accept()
        with client, server:
            client.send(b"one")
            client.send(b"two")
            assert server.recv(10) == b"one"
            assert server.recv(10) == b"two"
            server.send(b"back")
            assert client.recv(10) == b"back"
            assert listener.getsockname() == server.getsockname() == client.getpeername() == os.fsdecode(bound)
        file.remove()
        assert not os.path.lexists(p)
    assert e_left(options) == []


# A second bind at the same path fails as the standard's (EADDRINUSE for a
# file there), its E cleaned up, and the first listener's socket file stays.
def test_a_second_bind_fails_and_leaves_the_first(tmp: TmpDirs, options: sockat.ex.Options) -> None:
    p = long_path(tmp.new())
    listener, file, _ = listen_at(p, options)
    with listener:
        with raises_errno(standard_errno("bind-exists", SEQPACKET)):
            listen_at(p, options)
        assert is_socket(p)
        assert len(e_left(options)) == 1
        file.remove()
    assert e_left(options) == []


# A connect to a path with nothing there fails as the standard's (ENOENT),
# and its spelling is cleaned up.
def test_a_connect_to_nothing_fails(tmp: TmpDirs, options: sockat.ex.Options) -> None:
    p = long_path(tmp.new())
    with raises_errno(standard_errno("connect-missing", SEQPACKET)):
        connect_to(p, options)
    assert e_left(options) == []
