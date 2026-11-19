"""``sockat.ex.connect`` / ``sockat.ex.connect_nonblocking`` (hub SPEC "_ex",
DR-0015): the stream of the plain connect, and what the connect went
through (``losses``, ``route``, ``skipped``), on success and on the errors.
With ``last_resort`` the SPEC goes on to fork-cwd-direct, which this
implementation does not have: it is noted as skipped, and the connect
reaches what the plain one reaches."""

from __future__ import annotations

import asyncio
import errno
import os

import pytest
from _support import (
    NO_PROC,
    TmpDirs,
    accept_ping,
    assert_far_from_cwd,
    d_cfg,
    far_connect_here,
    listener_at,
    nest,
    stale_socket,
    with_name,
)

import sockat
import sockat.ex
from sockat import _route
from sockat._route import CONNECT, Config, Route, Trail
from sockat._sys import DIRECT_MAX
from sockat.ex import Connected, Skipped

CALLS = [
    pytest.param(sockat.ex.connect, True, id="connect"),
    pytest.param(sockat.ex.connect_nonblocking, False, id="connect_nonblocking"),
]


def far(tmp: TmpDirs, name: bytes = b"s.sock") -> bytes:
    p = nest(tmp.new(), 600, name)
    assert_far_from_cwd(p)
    return p


def names(skipped: tuple[Skipped, ...]) -> list[str]:
    """The names of ``skipped``, after checking every reason says something."""
    assert all(isinstance(s, Skipped) and s.reason for s in skipped), skipped
    return [s.name for s in skipped]


@pytest.fixture
def env_cfg(monkeypatch: pytest.MonkeyPatch) -> list[Config]:
    """Have the calls read the configuration put in the list (its last item)
    instead of the environment."""
    cfgs: list[Config] = []
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: cfgs[-1]))
    return cfgs


def nothing_reaches() -> Config:
    """No route of the connect order usable for a path far from the cwd with
    a file name past every sockaddr: no place for D or the symlink, no thread
    cwd, /proc made to look absent."""
    return d_cfg((), (), thread_cwd=False, proc=NO_PROC)


# What the calls return: stream, losses, route, skipped, by name or by
# position. A path the socket module takes goes to the standard: nothing lost
# or skipped. The stream is the plain call's (blocking for connect,
# nonblocking for connect_nonblocking), connected, and talks.
@pytest.mark.parametrize(("call", "blocking"), CALLS)
@pytest.mark.parametrize("last_resort", [False, True])
def test_what_the_calls_return(tmp: TmpDirs, call: object, blocking: bool, last_resort: bool) -> None:
    assert callable(call)
    p = nest(tmp.new(), 60)
    with listener_at(p) as listener:
        done = call(p, last_resort=last_resort)
        assert isinstance(done, Connected)
        stream, losses, route, skipped = done
        with stream:
            assert (done.stream, done.losses, done.route, done.skipped) == (stream, losses, route, skipped)
            assert (losses, route, skipped) == ((), "standard", ())
            assert stream.getblocking() is blocking
            accept_ping(listener, stream)


# A long path goes the way the plain connect goes (through the public API,
# from the environment), losing nothing: the route and the routes skipped
# before it are those the connect order notes for the same call, and the
# last resort changes nothing where a route carries it. The nonblocking
# stream is adopted by asyncio and reports the path as its peer.
@pytest.mark.parametrize("last_resort", [False, True])
def test_a_long_path_goes_the_plain_connects_way(tmp: TmpDirs, last_resort: bool) -> None:
    want = far_connect_here()
    if want is None:
        pytest.skip("no route of the connect order without a place for the symlink here")
    p = far(tmp)
    with listener_at(p) as listener:
        plain = _route.reach(CONNECT, p, Trail())
        with plain.sock as client:
            accept_ping(listener, client)
        stream, losses, route, skipped = sockat.ex.connect(p, last_resort=last_resort)
        with stream:
            assert (losses, route) == ((), want.value)
            assert names(skipped) == [r.value for r, _ in plain.skipped]
            assert stream.getpeername() == os.fsdecode(p)
            accept_ping(listener, stream)

        async def adopt() -> None:
            stream, _, _, _ = sockat.ex.connect_nonblocking(p, last_resort=last_resort)
            _, writer = await asyncio.open_unix_connection(sock=stream)
            assert writer.get_extra_info("peername") == os.fsdecode(p)
            writer.close()
            await writer.wait_closed()

        asyncio.run(adopt())
        listener.accept()[0].close()


# ----- the last resort -----


# Where no route of the connect order carries the path, both calls are
# ENAMETOOLONG with no route noted as having failed and every route of the
# order skipped after the standard. With the last resort, fork-cwd-direct is
# skipped last, for want of a fork route in sockat-py (SPEC: a language
# without fork notes it with a reason and goes on); without it, the order
# ends where the plain connect's does.
@pytest.mark.parametrize(("call", "blocking"), CALLS)
@pytest.mark.parametrize("last_resort", [False, True])
def test_where_nothing_reaches_the_last_resort_is_noted(
    tmp: TmpDirs, env_cfg: list[Config], call: object, blocking: bool, last_resort: bool
) -> None:
    assert callable(call)
    p = far(tmp, b"n" * (DIRECT_MAX + 3))
    env_cfg.append(nothing_reaches())
    with pytest.raises(OSError, match="too long") as e:
        call(p, last_resort=last_resort)
    assert e.value.errno == errno.ENAMETOOLONG
    assert getattr(e.value, "route") is None  # noqa: B009
    order = [r.value for r in _route.connect_order(False)]
    tail = [Route.FORK_CWD_DIRECT.value] if last_resort else []
    skipped = getattr(e.value, "skipped")  # noqa: B009
    assert names(skipped) == ["standard", *order, *tail]
    if last_resort:
        assert skipped[-1].reason == _route.NO_FORK


# ----- errors -----


# A connect that fails is the error the plain connect raises (its type and
# errno), with the routes skipped up to the failure and the route whose
# connect failed: the standard for a short path (nothing skipped), the
# connect order's route for a long one. A missing directory is ENOENT before
# any route (no route, nothing skipped), and an argument error is the
# ValueError, with nothing skipped and no route.
@pytest.mark.parametrize(("call", "blocking"), CALLS)
def test_a_failed_connect_tells_the_route_and_what_was_skipped(tmp: TmpDirs, call: object, blocking: bool) -> None:
    assert callable(call)
    short = nest(tmp.new(), 60)
    stale_socket(short)
    with pytest.raises(ConnectionRefusedError) as refused:
        call(short)
    assert (getattr(refused.value, "route"), getattr(refused.value, "skipped")) == ("standard", ())  # noqa: B009

    want = far_connect_here()
    if want is not None:
        long = far(tmp)
        stale_socket(long)
        with pytest.raises(ConnectionRefusedError) as refused:
            call(long)
        assert getattr(refused.value, "route") == want.value  # noqa: B009
        assert names(getattr(refused.value, "skipped"))[:1] == ["standard"]  # noqa: B009

    missing = with_name(far(tmp), b"missing/s.sock")
    with pytest.raises(FileNotFoundError) as gone:
        call(missing)
    assert (getattr(gone.value, "route"), getattr(gone.value, "skipped")) == (None, ())  # noqa: B009

    with pytest.raises(ValueError, match="NUL") as bad:
        call("/tmp/a\0b")
    assert (getattr(bad.value, "route"), getattr(bad.value, "skipped")) == (None, ())  # noqa: B009
    assert not os.path.lexists(missing)
