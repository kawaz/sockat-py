"""What happens before any route: the boundary of the paths the ``socket``
module takes (hub SPEC "短いパス"), the paths refused as argument errors
(NUL, a long path that does not end with a file name), and the socket's
directory looked up first (``ENOENT`` / ``ENOTDIR`` before anything is
judged or made)."""

from __future__ import annotations

import errno
import os
import socket

import pytest
from _support import (
    TmpDirs,
    accept_ping,
    connect_in,
    env_at,
    is_socket,
    listing,
    long_path,
    nest,
    raises_errno,
    tree,
)

import sockat
import sockat.ex
from sockat import _calls
from sockat._sockets import PathSocket
from sockat._sys import DIRECT_MAX, IS_LINUX


# A path of exactly the standard's length goes to the socket module as is:
# route "standard", a plain socket.socket, no B made (the environment is not
# even read). One byte more takes a route and the socket is a PathSocket. The
# boundary is the standard's own: socket.socket refuses DIRECT_MAX + 1.
@pytest.mark.parametrize("total", [DIRECT_MAX, DIRECT_MAX + 1])
def test_the_standards_length_goes_to_the_standard_and_one_byte_more_takes_a_route(tmp: TmpDirs, total: int) -> None:
    place = tmp.new()
    env = env_at(place)
    p = nest(tmp.new(), total)
    done, file = _calls.bind(p, env=env, remember=True)
    with done.sock as listener:
        if total <= DIRECT_MAX:
            assert done.route == "standard"
            assert type(listener) is socket.socket
            assert listing(place) == []
        else:
            assert done.route != "standard"
            assert type(listener) is PathSocket
            with socket.socket(socket.AF_UNIX) as s, pytest.raises(OSError, match="too long"):
                s.bind(p)
        for nonblocking in (False, True):
            client, route = connect_in(env, p, nonblocking=nonblocking)
            with client:
                assert (route == "standard") == (total <= DIRECT_MAX)
                assert client.getblocking() is not nonblocking
                accept_ping(listener, client)
        assert is_socket(p)
        assert file is not None
        file.remove()
    assert not os.path.lexists(p)


# A NUL anywhere but at the start is an argument error whatever the length
# (the socket module would cut the path there for a short one), for every
# call, and nothing is made.
@pytest.mark.parametrize("long", [False, True])
def test_a_nul_byte_is_an_argument_error_at_any_length(tmp: TmpDirs, long: bool) -> None:
    d = tmp.new()
    p = (long_path(d) if long else d + b"/s.sock").replace(b"s.sock", b"s\0.sock")
    before = tree(d)
    for call in (
        sockat.bind,
        sockat.connect,
        sockat.connect_nonblocking,
        sockat.datagram.bind,
        sockat.ex.bind,
        sockat.ex.connect,
        sockat.ex.resolve_for_bind,
        sockat.ex.resolve_for_connect,
    ):
        with pytest.raises(ValueError, match="NUL"):
            call(p)
    with sockat.datagram.unbound() as s, pytest.raises(ValueError, match="NUL"):
        sockat.datagram.connect(s, p)
    assert tree(d) == before


# A leading NUL is the abstract namespace on Linux: it goes to the socket
# module as is (no route, nothing made). Where there is no abstract
# namespace it is an argument error like any NUL.
def test_a_leading_nul_is_abstract_on_linux_and_an_argument_error_elsewhere() -> None:
    name = b"\0sockat-test-" + os.urandom(6).hex().encode()
    if not IS_LINUX:
        with pytest.raises(ValueError, match="NUL"):
            sockat.ex.bind(name)
        with pytest.raises(ValueError, match="NUL"):
            sockat.ex.bind(name + b"x" * 120)
        return
    bound = sockat.ex.bind(name)
    with bound.socket:
        assert bound.route == "standard"
        assert bound.spelling == sockat.ex.Original()
        c = sockat.ex.connect(name)
        with c.socket:
            assert c.route == "standard"
            accept_ping(bound.socket, c.socket)
        bound.file.remove()
    # Longer than sun_path: the socket module's own error, not a route.
    with pytest.raises(OSError, match="too long") as e:
        sockat.ex.bind(name + b"x" * 120)
    assert e.value.route == "standard"  # type: ignore[attr-defined]


# A long path that does not end with a file name is refused before anything
# is looked up or made: the kernel would take "dir/." as the directory itself.
@pytest.mark.parametrize("tail", [b"/", b"/.", b"/.."])
def test_a_long_path_not_ending_with_a_file_name_is_an_argument_error(tmp: TmpDirs, tail: bytes) -> None:
    place = tmp.new()
    d = os.path.dirname(long_path(tmp.new()))
    before = tree(d)
    with pytest.raises(ValueError, match="file name"):
        _calls.bind(d + tail, env=env_at(place))
    with pytest.raises(ValueError, match="file name"):
        connect_in(env_at(place), d + tail)
    with pytest.raises(ValueError, match="file name") as e:
        sockat.ex.bind(d + tail)
    assert e.value.skipped == ()  # type: ignore[attr-defined]
    assert e.value.route is None  # type: ignore[attr-defined]
    assert tree(d) == before
    assert listing(place) == []


# The socket's directory is looked up before any length is judged or
# anything is made: a missing one is ENOENT, a file in its place ENOTDIR, with
# no route tried (route None, skipped empty) and no B made.
@pytest.mark.parametrize(("make_file", "code"), [(False, errno.ENOENT), (True, errno.ENOTDIR)])
def test_the_directory_is_looked_up_first(tmp: TmpDirs, make_file: bool, code: int) -> None:
    place = tmp.new()
    d = tmp.new()
    if make_file:
        with open(d + b"/f", "wb"):
            pass
        p = d + b"/f/" + b"x" * 150 + b"/s.sock"
    else:
        p = d + b"/" + b"x" * 150 + b"/s.sock"
    with raises_errno(code):
        _calls.bind(p, env=env_at(place))
    with raises_errno(code):
        connect_in(env_at(place), p)
    for call in (sockat.ex.bind, sockat.ex.connect, sockat.ex.resolve_for_bind, sockat.ex.resolve_for_connect):
        with raises_errno(code) as e:
            call(p)
        assert e.value.skipped == ()  # type: ignore[attr-defined]
        assert e.value.route is None  # type: ignore[attr-defined]
    assert listing(place) == []
