"""A bind's symlink-file route (hub SPEC "symlink-file", DR-0016): right
after the standard, on every system but Linux / Android, a symlink
``<place>/sockat.XXXXXXXXXX`` to the socket file in a safe place, the bind
through it, the symlink removed again. Where the bind follows a symlink in
the last component (macOS) the socket file is made at the path whatever its
file name; where it does not (Linux, measured), the kernel answers without
making anything there, and the route is told unusable at run time and
skipped.

Linux does not try the route by default (``Config.bind_symlink_file``); the
tests turn it on there to see a kernel that does not follow the link. The
places are directories of the tests' own (0700, so safe) unless a test says
otherwise, and the paths are 600 bytes deep under /tmp (so the connects take
the thread's own cwd on macOS, proc-direct on Linux)."""

from __future__ import annotations

import errno
import os
import re
import sys

import pytest
from _support import (
    TmpDirs,
    accept_ping,
    assert_empty,
    assert_far_from_cwd,
    assert_untouched,
    bind_follows_a_symlink,
    check_route,
    d_cfg,
    d_route_here,
    far_connect_here,
    forced,
    is_socket,
    kernel_sockname,
    listing,
    mark_untouched,
    nest,
    parent,
    place,
    raises_errno,
    routes,
    stale_socket,
    with_name,
)

from sockat import _route
from sockat._route import BIND, OWN_DIR_PREFIX, RANDOM_LEN, Loss, Route, Trail
from sockat._sys import IS_FREEBSD, IS_LINUX

PROBED = (
    f"probe on {sys.platform}: a bind {'follows' if bind_follows_a_symlink() else 'does not follow'}"
    " a symlink in the last component"
)
"""What :func:`_support.bind_follows_a_symlink` found here, as one line. It is
the reason of every skip below, so the test run's summary (``-ra``, the
project's default) shows it on each system, the CI's VMs included (the SPEC
leaves it unconfirmed on FreeBSD, NetBSD, OpenBSD and illumos)."""

FOLLOWS = not IS_LINUX and bind_follows_a_symlink()
"""Whether the default bind here goes through symlink-file (tried, and the
bind follows the link)."""

follows = pytest.mark.skipif(not FOLLOWS, reason=PROBED + ("; Linux does not try the route" if IS_LINUX else ""))

LINK = re.escape(OWN_DIR_PREFIX) + rb"[A-Za-z0-9]{%d}" % RANDOM_LEN


def far(tmp: TmpDirs, name: bytes = b"s.sock") -> bytes:
    p = nest(tmp.new(), 600, name)
    assert_far_from_cwd(p)
    return p


def bind(p: bytes, cfg: _route.Config) -> tuple[_route.Reached, Trail]:
    trail = Trail()
    return _route.reach_long(BIND, p, cfg, trail), trail


# ----- where the bind follows the link -----


# Where the bind follows the link (macOS), the default bind of a long path
# goes through symlink-file, losing respelled only, with only the standard
# skipped; the route gets every check of a route (check_route: the socket at
# the path, data both ways, blocking and nonblocking connects, the path
# reported while the kernel's address fits sun_path, asyncio serving and
# cleaning up as for the standard's, nothing left in the place for the
# symlink nor in the place for D the connects use). The kernel keeps
# `<place>/sockat.XXXXXXXXXX`, a symlink gone by then: unlinking that
# spelling removes nothing (FileNotFoundError), and the socket file at the
# path is left.
@follows
def test_where_the_bind_follows_the_link_it_carries_a_long_path(tmp: TmpDirs) -> None:
    p = far(tmp)
    links = tmp.new()
    d_place = tmp.new()
    c = d_cfg((d_place,), [links])
    done, _ = bind(p, c)
    with done.sock as listener:
        assert done.route is Route.SYMLINK_FILE
        assert done.losses == (Loss.RESPELLED,)
        assert [r for r, _ in done.skipped] == [Route.STANDARD]
        kernel = os.fsencode(kernel_sockname(listener))
        assert re.fullmatch(re.escape(links) + b"/" + LINK, kernel), kernel
        with pytest.raises(FileNotFoundError):
            os.unlink(kernel)
        assert is_socket(p)
    os.unlink(p)
    assert_empty(links)
    connect_route = far_connect_here()
    assert connect_route is not None
    check_route(forced("symlink-file", p, c, routes(Route.SYMLINK_FILE, connect_route)), [links, d_place])


# The file name has no limit through symlink-file: names of 104 and 254
# bytes (past every sockaddr; NAME_MAX is 255) are bound at the path, which
# the listener reports.
@follows
@pytest.mark.parametrize("n", [104, 254])
def test_through_the_link_any_file_name_is_bound(tmp: TmpDirs, n: int) -> None:
    p = far(tmp, b"n" * n)
    links = tmp.new()
    done, _ = bind(p, d_cfg((), [links]))
    with done.sock as listener:
        assert done.route is Route.SYMLINK_FILE
        assert is_socket(p)
        assert listener.getsockname() == os.fsdecode(p)
    assert_empty(links)


# A relative path (here from the test process's cwd, the project root, down
# to /tmp) is bound through a link to the path made absolute against the cwd:
# the link sits in the place, so a relative target would be resolved from
# there and miss the directory. The listener reports the path as given.
@follows
def test_a_relative_path_is_bound_through_its_absolute_target(tmp: TmpDirs) -> None:
    p = far(tmp)
    rel = os.fsencode(os.path.relpath(p))
    assert not rel.startswith(b"/")
    links = tmp.new()
    done, _ = bind(rel, d_cfg((), [links]))
    with done.sock as listener:
        assert done.route is Route.SYMLINK_FILE
        assert is_socket(p)
        assert listener.getsockname() == os.fsdecode(rel)
    assert_empty(links)


# ----- where the bind does not follow it -----


# Where the bind does not follow the link (Linux, the route turned on there),
# the kernel answers without making anything at the path: the route is told
# unusable and skipped (after the standard), nothing is left in the place or
# next to the socket, and the bind goes on to the routes through D (or ends
# in ENAMETOOLONG where they have no means). Where the bind follows it, the
# same configuration goes through the link.
@pytest.mark.skipif(FOLLOWS, reason=PROBED)
def test_where_the_bind_does_not_follow_the_link_it_is_skipped(tmp: TmpDirs) -> None:
    p = far(tmp)
    links = tmp.new()
    c = d_cfg((tmp.new(),), [links], bind_symlink_file=True)
    here = d_route_here()
    if here is None:
        with raises_errno(errno.ENAMETOOLONG):
            bind(p, c)
        assert listing(parent(p)) == []
    else:
        done, _ = bind(p, c)
        with done.sock as listener:
            assert done.route is here
            names = [r for r, _ in done.skipped]
            assert names[:2] == [Route.STANDARD, Route.SYMLINK_FILE]
            assert dict(done.skipped)[Route.SYMLINK_FILE] == "symlink not followed by bind"
            with _route.run_long(_route.CONNECT, p, c)[0] as client:
                accept_ping(listener, client)
    assert_empty(links)


# On Linux the route is not in the bind's order by default: it is neither
# tried nor noted as skipped (SPEC: a route not in the system's order is not
# noted), and nothing is made in the place for its symlink.
@pytest.mark.skipif(not IS_LINUX, reason="Linux does not try the route")
def test_linux_does_not_try_the_route(tmp: TmpDirs) -> None:
    p = far(tmp)
    links = tmp.new()
    mark_untouched(links)
    c = d_cfg((tmp.new(),), [links])
    assert not c.bind_symlink_file
    here = d_route_here()
    if here is None:
        return
    done, _ = bind(p, c)
    with done.sock:
        assert Route.SYMLINK_FILE not in [r for r, _ in done.skipped]
    assert_untouched(links)


# ----- failures and places -----


# Something already at the path (a socket nobody listens on, a plain file)
# makes the bind through the link fail with EADDRINUSE whether or not the
# kernel followed it: the path is looked up, the thing is there, and the
# failure is the outcome, with symlink-file noted as the route that failed.
# Nothing after it is tried (the place for D stays untouched), the link is
# removed, and what was at the path is left as it was.
@pytest.mark.parametrize("what", ["stale-socket", "file"])
def test_something_at_the_path_is_eaddrinuse_and_not_retried(tmp: TmpDirs, what: str) -> None:
    p = far(tmp)
    if what == "stale-socket":
        stale_socket(p)
    else:
        with open(p, "wb") as f:
            f.write(b"theirs")
    links = tmp.new()
    d_place = tmp.new()
    mark_untouched(d_place)
    c = d_cfg((d_place,), [links], bind_symlink_file=True)
    trail = Trail()
    with raises_errno(errno.EADDRINUSE):
        _route.reach_long(BIND, p, c, trail)
    assert trail.trying is Route.SYMLINK_FILE
    assert [r for r, _ in trail.skipped] == [Route.STANDARD]
    assert_empty(links)
    assert_untouched(d_place)
    assert listing(parent(p)) == [os.path.basename(p)]
    if what == "file":
        with open(p, "rb") as f:
            assert f.read() == b"theirs"
    else:
        assert is_socket(p)


# The places are those of a connect's symlink-file and are judged the same
# way: one that is missing, too long for the spelling, or not safe (others
# may write to it, not sticky) is passed over with nothing made in it, and
# the next carries the bind. With none left, the route is skipped as having
# no place.
@follows
def test_a_place_that_cannot_carry_it_is_passed_over(tmp: TmpDirs) -> None:
    p = far(tmp)
    t = tmp.new()
    unsafe = place(t, b"open", 0o777)
    too_long = parent(nest(tmp.new(), 120, b"x"))
    good = tmp.new()
    for d in (unsafe, too_long):
        mark_untouched(d)
    done, _ = bind(p, d_cfg((), [b"/nonexistent", unsafe, too_long, good]))
    with done.sock:
        assert done.route is Route.SYMLINK_FILE
    for d in (unsafe, too_long):
        assert_untouched(d)
    assert_empty(good)
    os.unlink(p)

    q = with_name(p, b"q.sock")
    trail = Trail()
    c = d_cfg((), [unsafe], thread_cwd=False)
    if IS_FREEBSD:
        # bindat cannot be turned off: a later route carries the bind.
        done, trail = bind(q, c)
        with done.sock:
            noted = list(done.skipped)
    else:
        with raises_errno(errno.ENAMETOOLONG):
            _route.reach_long(BIND, q, c, trail)
        noted = list(trail.skipped)
    assert dict(noted)[Route.SYMLINK_FILE] == _route.NO_PLACE_FOR_LINK
    assert_untouched(unsafe)
