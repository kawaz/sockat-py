"""The relative route of a connect (every system but FreeBSD and Linux
with /proc; made so on Linux here by pointing ``proc_self_fd`` at a path that
does not exist), the fork route's place, and opening the socket's directory.
The routes through D are in ``test_through_d.py``, the symlink route in
``test_symlink.py``.

Tests that need a particular cwd or ``$PWD`` run in a child process
(``_children.py``)."""

from __future__ import annotations

import errno
import fcntl
import os
import sys
from collections.abc import Callable

import pytest
from _support import (
    NO_PLACE,
    TmpDirs,
    accept_ping,
    assert_far_from_cwd,
    assert_untouched,
    check_unreachable,
    d_cfg,
    dir_id,
    fallback_cfg,
    is_socket,
    listener_at,
    listing,
    mark_untouched,
    needs_route_through_d,
    nest,
    nest_tail,
    parent,
    raises_errno,
    run_child,
    stale_socket,
    with_name,
)

from sockat import _route, _sys
from sockat._route import BIND, CONNECT, CONNECT_NONBLOCKING, Route
from sockat._sys import DIRECT_MAX, IS_APPLE, WIDE_PATH_MAX

FREEBSD = sys.platform.startswith("freebsd")
no_relative = pytest.mark.skipif(FREEBSD, reason="FreeBSD's connect falls back to connectat only")


# ----- the relative spelling -----


# A connect to a path over DIRECT_MAX bytes whose spelling relative to the
# cwd is at most DIRECT_MAX bytes takes the relative fallback (the spelling
# handed to the socket module), before anything else (on Apple, before the
# widened form). Every check of a route (the socket at the given absolute
# path, data both ways, blocking and nonblocking connects, close-on-exec, the
# peer is this process, the cwd unchanged, the path reported as the peer,
# asyncio adopting the connects; a nonblocking connect fails at once on a full
# backlog) is run on it, in a child whose cwd is the socket's ancestor. The
# listener is bound by the routes through D: a bind never takes the relative
# spelling.
#
# On Apple the connects also take the widened form with the relative spelling
# (a path over 253 bytes whose relative spelling is over DIRECT_MAX and at
# most 253) and get the same checks.
@no_relative
@needs_route_through_d
def test_a_path_short_relative_to_the_cwd_takes_the_relative_fallback(tmp: TmpDirs) -> None:
    t = tmp.new()
    args = {"PATH": nest_tail(t, DIRECT_MAX, b"rel.sock")}
    assert len(args["PATH"]) > DIRECT_MAX
    want = "relative"
    if IS_APPLE:
        args["WPATH"] = nest_tail(t, 240, b"wrel.sock")
        assert len(args["WPATH"]) > WIDE_PATH_MAX
        want = "relative,widened-relative"
    out = run_child("relative_routes", t, args)
    assert out == {"checked": want}


def connect_in_child(cwd: bytes, pwd: bytes, path: bytes) -> str:
    """Connect to a listener at ``path`` in a child whose cwd is ``cwd`` and
    ``$PWD`` is ``pwd``, by the fallbacks alone; the route it reported. The
    listener accepts the connection, so it went where the path points."""
    with listener_at(path) as listener:
        out = run_child("connect_report", cwd, {"PATH": path}, env={"PWD": pwd})
        listener.setblocking(False)
        listener.accept()[0].close()
    return out["route"]


def real_and_link(t: bytes) -> tuple[bytes, bytes]:
    """``<t>/real/`` and ``<t>/link -> real``, for the cwd tests."""
    real = t + b"/real"
    os.mkdir(real)
    os.symlink(b"real", t + b"/link")
    return real, t + b"/link"


# The relative spelling is worked out from the cwd's spellings and checked to
# name the socket's directory, whatever way the cwd and the path are written;
# each shape is checked by the spelling (Relative.new, in a child with that
# cwd and $PWD, see the scenario) and by a connect that reaches a listener at
# the path:
#
# * $PWD reached through a symlink, and the path written through it: the
#   prefix is cut off and the spelling only descends (no "..");
# * the cwd reached through a symlink, $PWD not naming it ("/"): both sides
#   resolved to real paths;
# * a symlink in the middle of the path, below the cwd: the spelling descends
#   through it, no "..";
# * the path in a sibling of the cwd: "../../c/...";
# * a $PWD that names another directory than the cwd (with the same
#   subtree): not used, the spelling climbs out of the real cwd to where the
#   path points;
# * a cwd whose name is only a byte prefix of the path's next component
#   (<t>/ab against <t>/abc/...): not cut off, the spelling climbs.
@no_relative
@pytest.mark.parametrize("shape", ["pwd-link", "cwd-link", "middle-link", "sibling", "stale-pwd", "byte-prefix"])
def test_the_relative_spelling_names_the_sockets_directory(tmp: TmpDirs, shape: str) -> None:
    t = tmp.new()
    check: Callable[[bytes], bool]
    if shape in ("pwd-link", "cwd-link"):
        _, link = real_and_link(t)
        p = nest_tail(link, 95)
        cwd, pwd = link, (link if shape == "pwd-link" else b"/")
        below = p[len(link) + 1 :]

        def check(rel: bytes) -> bool:
            return rel == below

    elif shape == "middle-link":
        _, link = real_and_link(t)
        p = nest_tail(link, 90)
        cwd = pwd = t

        def check(rel: bytes) -> bool:
            return b".." not in rel.split(b"/")

    elif shape == "sibling":
        cwd = pwd = t + b"/a/b"
        os.makedirs(cwd)
        p = nest_tail(t + b"/c", 90)

        def check(rel: bytes) -> bool:
            return rel.startswith(b"../../c/")

    elif shape == "stale-pwd":
        cwd, pwd = t + b"/one", t + b"/two"
        p = nest_tail(pwd, 90)
        os.makedirs(cwd + b"/" + os.path.dirname(p[len(pwd) + 1 :]))

        def check(rel: bytes) -> bool:
            return rel.startswith(b"../two/")

    else:
        cwd = pwd = t + b"/ab"
        os.mkdir(cwd)
        p = nest_tail(t + b"/abc", 90)

        def check(rel: bytes) -> bool:
            return rel.startswith(b"../abc/")

    assert len(p) > DIRECT_MAX
    assert connect_in_child(cwd, pwd, p) == Route.RELATIVE.value
    out = run_child("relative_spelling", cwd, {"PATH": p}, env={"PWD": pwd})
    assert check(os.fsencode(out["rel"])), out


# Another thread that changes the cwd between the spelling and the connect
# sends the call to another directory. When the cwd is still changed
# afterwards the result is discarded and the error is a general one (an
# OSError without errno), even though the call itself succeeded: a stream to
# the wrong server must not reach the caller as if it were at the path they
# gave. The connected stream is closed, which its server sees as end of file.
# Runs in a child process because it changes the cwd; see the scenario.
@no_relative
def test_a_cwd_changed_during_a_relative_connect_is_a_general_error(tmp: TmpDirs) -> None:
    t = tmp.new()
    one = t + b"/one"
    two = t + b"/two"
    p = nest_tail(one, 90)
    sub = p[len(one) + 1 :]
    os.makedirs(two + b"/" + os.path.dirname(sub))
    run_child("cwd_changed", one, {"PATH": p, "OTHER": two})


# A failure on the relative fallback is the kernel's error for the path
# (ENOENT for a missing socket, ECONNREFUSED for a socket nobody listens on),
# for both connects, and nothing after it is tried: the place for the symlink
# stays untouched. Runs in a child whose cwd is near the socket.
@no_relative
def test_a_failure_on_the_relative_fallback_is_not_retried(tmp: TmpDirs) -> None:
    t = tmp.new()
    base = tmp.new()
    p = nest_tail(t, DIRECT_MAX)
    # The same length as p, so its relative spelling fits too.
    stale = with_name(p, b"x.sock")
    stale_socket(stale)
    mark_untouched(base)
    out = run_child("relative_failures", t, {"PATH": p, "BASE": base, "STALE": stale})
    assert out == {
        "no-socket False": str(errno.ENOENT),
        "no-socket True": str(errno.ENOENT),
        "stale False": str(errno.ECONNREFUSED),
        "stale True": str(errno.ECONNREFUSED),
    }
    assert_untouched(base)


# A bind never takes the relative spelling: the spelling would stay with the
# listener and name another file once the cwd changes. A path whose relative
# spelling fits, with the routes through D and the form without D unusable (no
# place for D, no means), is ENAMETOOLONG for the bind with nothing created, while a connect to a
# listener there takes the relative fallback. Runs in a child whose cwd is
# the socket's directory's ancestor.
@no_relative
def test_a_bind_never_takes_the_relative_spelling(tmp: TmpDirs) -> None:
    t = tmp.new()
    p = nest_tail(t, DIRECT_MAX, b"b.sock")
    before = listing(parent(p))
    out = run_child("bind_with_no_route", t, {"PATH": p})
    assert out == {"errno": str(errno.ENAMETOOLONG)}
    assert listing(parent(p)) == before
    assert connect_in_child(t, t, p) == Route.RELATIVE.value


# ----- the fork route's place -----


# This implementation has no fork route (a child forked from Python runs
# Python, which is not async-signal-safe). A bind neither the routes through D
# nor the form without D carries (here: no means, no place for D at all) ends
# there with ENAMETOOLONG, the error
# of a plain bind of a path too long for sun_path, with nothing created next
# to the socket; so do the connects that no fallback carries (no usable place
# for a symlink: none, one that does not exist, one too long for a spelling
# through it). Nothing is created in the place either. With a place for D the
# same path is bound where the routes through D can be used. FreeBSD is left
# out: bindat / connectat cannot be turned off there, so the bind (the form
# without D) and the connect (connectat) reach the socket.
@pytest.mark.skipif(sys.platform.startswith("freebsd"), reason="bindat / connectat reach the socket on FreeBSD")
def test_where_only_the_fork_route_reaches_everything_is_enametoolong(tmp: TmpDirs) -> None:
    t = tmp.new()
    p = nest(t, 600)
    assert_far_from_cwd(p)
    long_place = nest(tmp.new(), 250, b"place")
    os.mkdir(long_place)
    for bases in ([], [NO_PLACE], [long_place]):
        mark_untouched(long_place)
        check_unreachable(p, fallback_cfg(bases), [long_place])


# The counterpart of the test above: the same kind of path, with the
# socket's directory as the place for D, is bound and connected to (where the
# routes through D have a means).
@needs_route_through_d
def test_with_a_place_for_d_the_same_path_is_bound(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 600)
    listener, r = _route.run_long(BIND, p, d_cfg())
    with listener:
        assert r is not Route.STANDARD
        client, _ = _route.run_long(CONNECT, p, d_cfg())
        with client:
            accept_ping(listener, client)


# A real failure stays what it is: a missing directory is ENOENT, not
# ENAMETOOLONG, for the bind and both connects, whatever the file name's
# length (6 bytes, and 254, longer than any sockaddr carries) and whatever is
# configured: the directory is opened first, so its error comes before any
# length is judged.
def test_a_missing_directory_is_enoent_before_any_length_is_judged(tmp: TmpDirs) -> None:
    t = tmp.new()
    directory = parent(nest(t, 600))
    for n in (6, 254):
        missing = directory + b"/nope/" + b"n" * n
        for c in (fallback_cfg(), d_cfg()):
            for req in (BIND, CONNECT, CONNECT_NONBLOCKING):
                with pytest.raises(FileNotFoundError):
                    _route.run_long(req, missing, c)
    assert listing(directory) == []


# The bindat route (FreeBSD) builds the sockaddr of its spelling from what it
# is given; a spelling too long for a sockaddr is ENAMETOOLONG before
# bindat(2) would be called, so this is checked on any system.
def test_the_at_route_refuses_a_spelling_too_long_for_a_sockaddr(tmp: TmpDirs) -> None:
    d = tmp.new()
    fd = os.open(d, os.O_RDONLY)
    try:
        for req in (BIND, CONNECT, CONNECT_NONBLOCKING):
            with raises_errno(errno.ENAMETOOLONG):
                _route.at_op(req, fd, b"n" * (DIRECT_MAX + 1))
    finally:
        os.close(fd)
    assert listing(d) == []


# ----- opening the directory -----


# A kernel that does not take O_SEARCH on a directory answers EISDIR (as macOS
# does for O_EXEC alone) or EINVAL. The directory must then be opened O_RDONLY
# instead, rather than failing on a directory the route could use. The running
# kernel takes O_SEARCH, so the refusal is stood in for by the function handed
# to search_or_read_only. Checked:
#
# * EISDIR and EINVAL give an fd of that directory, opened O_RDONLY;
# * any other error from the O_SEARCH open is the real outcome and is raised
#   without trying O_RDONLY: an EACCES for a readable directory stays EACCES
#   (falling back would hide a real permission failure), as does ENOENT.
def test_a_kernel_refusing_o_search_falls_back_to_o_rdonly(tmp: TmpDirs) -> None:
    d = tmp.new()

    def refused(code: int) -> Callable[[], int]:
        def search() -> int:
            raise OSError(code, os.strerror(code))

        return search

    for code in (errno.EISDIR, errno.EINVAL):
        fd = _route.search_or_read_only(d, refused(code))
        try:
            st = os.fstat(fd)
            assert (st.st_dev, st.st_ino) == dir_id(d)
            assert fcntl.fcntl(fd, fcntl.F_GETFL) & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR) == os.O_RDONLY
        finally:
            os.close(fd)
    for code in (errno.EACCES, errno.ENOENT):
        with raises_errno(code):
            _route.search_or_read_only(d, refused(code))


# The fallback really is O_RDONLY: in a directory of mode 0300 (write +
# search, no read), which O_SEARCH (Apple) and O_PATH (Linux) open, the
# fallback fails with EACCES, and that is the error returned, not swallowed.
@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the permission under test")
@pytest.mark.skipif(not _sys.HAS_THREAD_CWD, reason="O_SEARCH / O_PATH are the Apple / Linux ways of opening")
def test_the_o_rdonly_fallback_needs_read_permission(tmp: TmpDirs) -> None:
    locked = tmp.new() + b"/locked"
    os.mkdir(locked, 0o300)
    os.chmod(locked, 0o300)
    os.close(_route.open_dir(locked))

    def refused() -> int:
        raise OSError(errno.EISDIR, os.strerror(errno.EISDIR))

    with pytest.raises(PermissionError):
        _route.search_or_read_only(locked, refused)


# Only binds' sockets land at a path: no route of a connect creates anything
# next to the socket, and the bind's routes through D leave only the socket
# file there (D and R in the socket's directory are removed). Checked with the
# socket's directory as the only place for D.
@needs_route_through_d
def test_nothing_but_the_socket_file_is_left_in_its_directory(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 600)
    listener, _ = _route.run_long(BIND, p, d_cfg())
    with listener:
        for req in (CONNECT, CONNECT_NONBLOCKING):
            client, _ = _route.run_long(req, p, d_cfg())
            with client:
                accept_ping(listener, client)
        assert listing(parent(p)) == [b"s.sock"]
    assert is_socket(p)
