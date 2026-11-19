"""The public API as a user sees it: ``sockat.bind`` / ``connect`` /
``connect_nonblocking`` with paths of any length, on whatever route the
system picks with the real environment (``$TMPDIR``, then ``/tmp``). Which
route ran is not observable through the API; the route-forcing tests are in
``test_routes_*.py``."""

from __future__ import annotations

import errno
import os
import socket
import stat
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from _support import (
    NAME_MAX_THROUGH_D,
    SO_NOSIGPIPE,
    TmpDirs,
    accept_ping,
    d_route_here,
    dir_id,
    is_cloexec,
    is_socket,
    listener_at,
    listing,
    needs_long_bind,
    nest,
    parent,
    ping,
    raises_errno,
    recv_exact,
    run_child,
    stale_socket,
    with_name,
)

import sockat
from sockat import _route
from sockat._route import BIND, CONNECT, Route
from sockat._sockets import PathSocket
from sockat._sys import DIRECT_MAX


class BytesPathLike:
    """An ``os.PathLike[bytes]`` (pathlib only makes ``os.PathLike[str]``)."""

    def __init__(self, p: bytes) -> None:
        self._p = p

    def __fspath__(self) -> bytes:
        return self._p


# ----- the boundary between the standard and the routes -----


# The contract that every path the socket module accepts behaves exactly like
# the socket module: at most DIRECT_MAX bytes goes to it unchanged, one byte
# more does not. DIRECT_MAX is the module's own limit, checked here against the
# module itself rather than a table: it binds and connects a path of DIRECT_MAX
# bytes and refuses one byte more with an OSError that has no errno ("AF_UNIX
# path too long", from `len >= sizeof(sun_path)` in socketmodule.c). So no path
# the standard can take is sent down another route, and none that it refuses is
# handed to it.
def test_a_path_the_standard_takes_goes_to_it_and_one_byte_more_takes_a_route(tmp: TmpDirs) -> None:
    t = tmp.new()
    at_max = os.fsdecode(nest(t, DIRECT_MAX, b"a.sock"))
    std_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with std_listener:
        std_listener.bind(at_max)
        std_listener.listen()
        std_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with std_client:
            std_client.connect(at_max)
            accept_ping(std_listener, std_client)
    over = os.fsdecode(nest(t, DIRECT_MAX + 1, b"o.sock"))
    for call in ("bind", "connect"):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with s, pytest.raises(OSError, match="AF_UNIX path too long") as e:
            getattr(s, call)(over)
        assert e.value.errno is None

    # At the limit sockat hands the path to the socket module as written (a
    # str stays a str), so the address the socket reports is the standard's.
    p = os.fsdecode(nest(t, DIRECT_MAX, b"s.sock"))
    listener, r = _route.run(BIND, p)
    with listener:
        assert r is Route.STANDARD
        assert listener.getsockname() == p
        client, r = _route.run(CONNECT, p)
        with client:
            assert r is Route.STANDARD
            assert client.getpeername() == p
            accept_ping(listener, client)

    # One byte more takes a route and still ends up at the given path (where
    # the routes through D have a means; elsewhere no long path can be bound).
    if d_route_here() is None:
        return
    q = nest(t, DIRECT_MAX + 1, b"s.sock")
    listener, r = _route.run(BIND, q)
    with listener:
        assert r is not Route.STANDARD
        assert is_socket(q)
        client, _ = _route.run(CONNECT, q)
        with client:
            accept_ping(listener, client)


# A path the socket module takes goes to it "before anything else is looked
# at": no environment (TMPDIR, PWD) is read and no relative spelling is worked
# out. Shown by making both of those fail: a short path still binds and
# connects.
def test_a_short_path_reads_nothing_from_the_environment(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    def no(*_: object) -> None:
        raise AssertionError("looked at the environment for a short path")

    monkeypatch.setattr(_route.Config, "from_env", no)
    monkeypatch.setattr(_route.Relative, "new", no)
    p = nest(tmp.new(), DIRECT_MAX)
    with sockat.bind(p) as listener, sockat.connect(p) as client:
        accept_ping(listener, client)


# ----- any length -----


# The promise of the package: a socket can be bound and reached at a path of
# any length (well past sun_path's 104 / 108 bytes), the socket file is at that
# path, it stays there when the listener is closed (as with the standard: the
# file is the caller's to remove), and it can be removed by that path. Both
# sides of the standard's length are included, and two bases: TMPDIR as the
# test runs with it (on macOS a long per-session directory) and /tmp.
@pytest.mark.parametrize("total", [100, DIRECT_MAX, DIRECT_MAX + 1, 108, 200, 253, 254, 400, 1000])
@needs_long_bind
def test_bind_and_connect_work_at_any_length(tmp: TmpDirs, total: int) -> None:
    bases = [tmp.new(), tmp.new(under=tempfile.gettempdir())]
    for base in bases:
        n = max(total, len(base) + 10)
        p = nest(base, n)
        listener = sockat.bind(p)
        with listener:
            assert is_socket(p), f"{n}: {p!r}"
            with sockat.connect(p) as client:
                accept_ping(listener, client)
            with sockat.connect_nonblocking(p) as client:
                accept_ping(listener, client)
        assert is_socket(p), "closing the listener leaves the socket file"
        os.unlink(p)
        assert not is_socket(p)


# The path can be given the ways Python file APIs take one: str, bytes, an
# os.PathLike[str] (pathlib) and an os.PathLike[bytes], for a short path (the
# standard's socket module itself does not take os.PathLike, sockat does, by
# os.fspath) and a long one.
@pytest.mark.parametrize("total", [60, 300])
@needs_long_bind
def test_paths_are_str_bytes_or_path_like(tmp: TmpDirs, total: int) -> None:
    t = tmp.new()
    makers: list[Callable[[bytes], str | bytes | os.PathLike[str] | os.PathLike[bytes]]] = [
        os.fsdecode,
        bytes,
        lambda b: Path(os.fsdecode(b)),
        BytesPathLike,
    ]
    for i, make in enumerate(makers):
        p = nest(t, total, f"{i}.sock".encode())
        given = make(p)
        with sockat.bind(given) as listener:
            assert is_socket(p)
            with sockat.connect(given) as client:
                accept_ping(listener, client)
            with sockat.connect_nonblocking(given) as client:
                accept_ping(listener, client)


# A file name longer than the D routes' `R/<name>` carries
# (NAME_MAX_THROUGH_D + 1 bytes), one longer than the plain sockaddr carries
# (DIRECT_MAX + 1) and one of 254 bytes (NAME_MAX is 255), directly under a
# short directory.
#
# * bind: the routes through D carry the file name in a sockaddr behind `R/`
#   (or behind /proc/self/fd/<n>/R/, shorter still), and no other bind route
#   is left (no fork route in Python; the bind never takes the widened
#   sockaddr, macOS included), so ENAMETOOLONG, and nothing is created.
# * connect: the fallbacks reach it: the link to the socket file is spelled
#   without the file name, /proc and the proc-held route on Linux, the widened
#   sockaddr on macOS. A socket bound under a short name and renamed to the
#   long one is reached and talks; with nothing there the error is the
#   kernel's ENOENT, not ENAMETOOLONG. FreeBSD falls back to connectat of the
#   file name, which a sockaddr carries up to DIRECT_MAX bytes: past that,
#   ENAMETOOLONG for the connect too.
@pytest.mark.parametrize("n", [NAME_MAX_THROUGH_D + 1, DIRECT_MAX + 1, 254])
def test_a_file_name_longer_than_the_routes_through_d_carries_cannot_be_bound_but_can_be_connected_to(
    tmp: TmpDirs, n: int
) -> None:
    t = tmp.new()
    p = t + b"/" + b"n" * n
    with raises_errno(errno.ENAMETOOLONG):
        sockat.bind(p)
    assert listing(t) == []
    if sys.platform.startswith("freebsd") and n > DIRECT_MAX:
        with raises_errno(errno.ENAMETOOLONG):
            sockat.connect(p)
        return
    with pytest.raises(FileNotFoundError):
        sockat.connect(p)
    with listener_at(p) as listener:
        with sockat.connect(p) as client:
            accept_ping(listener, client)
        with sockat.connect_nonblocking(p) as client:
            accept_ping(listener, client)


# Where the routes through D have no means (NetBSD, OpenBSD, illumos: no thread
# cwd, no bindat, no /proc), no long path can be bound (the SPEC's next step
# is the fork route, which Python does not have): ENAMETOOLONG with nothing
# created. A connect to a listener at such a path still gets through its
# fallbacks.
@pytest.mark.skipif(d_route_here() is not None, reason="the routes through D have a means here")
def test_without_the_routes_through_d_a_long_bind_is_enametoolong(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 300)
    with raises_errno(errno.ENAMETOOLONG):
        sockat.bind(p)
    assert listing(parent(p)) == []
    with listener_at(p) as listener, sockat.connect(p) as client:
        accept_ping(listener, client)


# ----- errors -----


# The documented errors for a long path through the public API, as the OSError
# subclasses Python maps their errno to (so `except FileNotFoundError` works as
# with the standard): ENOENT for a missing directory (bind and both connects)
# and for a missing socket, EADDRINUSE for an existing file, ECONNREFUSED for a
# socket file nobody listens on. A route must not turn these into "try the next
# route" and end up reporting something else (or creating the socket
# elsewhere).
@pytest.mark.parametrize("total", [150, 300, 600])
@needs_long_bind
def test_long_path_errors_are_the_kernels(tmp: TmpDirs, total: int) -> None:
    t = tmp.new()
    p = nest(t, total)
    missing = parent(p) + b"/nope/s.sock"
    for call in (sockat.bind, sockat.connect, sockat.connect_nonblocking):
        with pytest.raises(FileNotFoundError):
            call(missing)
    with pytest.raises(FileNotFoundError):
        sockat.connect(p)
    with sockat.bind(p), raises_errno(errno.EADDRINUSE):
        sockat.bind(p)
    with open(with_name(p, b"file"), "wb"):
        pass
    with raises_errno(errno.EADDRINUSE):
        sockat.bind(with_name(p, b"file"))
    stale = with_name(p, b"stale.sock")
    stale_socket(stale)
    for connect in (sockat.connect, sockat.connect_nonblocking):
        with pytest.raises(ConnectionRefusedError):
            connect(stale)


# A long path that does not end with a file name ("dir/", "dir/.", "dir/..")
# names a directory; binding it by "the last component" would create a socket
# named after the directory, which is not what the path says. It is refused as
# an argument error (ValueError) before anything is created, for bind and both
# connects.
def test_a_long_path_without_a_file_name_is_a_value_error(tmp: TmpDirs) -> None:
    t = tmp.new()
    directory = parent(nest(t, 150))
    before = listing(directory)
    for bad in (directory + b"/", directory + b"/.", directory + b"/.."):
        for call in (sockat.bind, sockat.connect, sockat.connect_nonblocking):
            with pytest.raises(ValueError, match="file name"):
                call(bad)
    assert listing(directory) == before


# A NUL byte in a path, at any length, is an argument error (ValueError) and
# nothing is created. The standard socket module does not refuse it: it hands
# the bytes to the kernel, which stops at the NUL, so `/tmp/x/a\0b` would bind
# `/tmp/x/a`, a different file (checked here against the module itself, as the
# premise of the rule). Short paths are no exception to this, although they go
# to the standard otherwise.
def test_a_nul_byte_is_a_value_error_at_any_length(tmp: TmpDirs) -> None:
    t = tmp.new()
    std = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with std:
        std.bind(t + b"/a\0b")
    assert listing(t) == [b"a"], "the premise: the standard binds the path cut at the NUL"
    os.unlink(t + b"/a")

    directory = parent(nest(t, 150))
    before = listing(t)
    cases: list[str | bytes] = [
        t + b"/c\0d",
        os.fsdecode(t + b"/c\0d"),
        directory + b"/c\0d",
        directory + b"\0/s.sock",
    ]
    for bad in cases:
        for call in (sockat.bind, sockat.connect, sockat.connect_nonblocking):
            with pytest.raises(ValueError, match="NUL"):
                call(bad)
    assert listing(t) == before
    assert listing(directory) == []


# On Linux a path that starts with a NUL is not a pathname but an address in
# the abstract namespace, and goes to the socket module as it is, whatever its
# length: code that uses abstract sockets with the standard keeps working when
# it switches to sockat. Shown with names of 50 bytes and of the longest the
# standard takes (108 bytes with the leading NUL, over DIRECT_MAX), and one
# byte more, which the standard refuses ("AF_UNIX path too long"), as sockat
# does since it hands the address over unchanged.
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the abstract namespace is Linux's")
def test_a_leading_nul_on_linux_is_an_abstract_address_for_the_standard() -> None:
    for n in (50, 108):
        addr = b"\0sockat-test-" + os.urandom(8).hex().encode()
        addr = addr + b"x" * (n - len(addr))
        assert len(addr) == n
        with sockat.bind(addr) as listener:
            # The standard's socket, nothing added: the address is the kernel's.
            assert type(listener) is socket.socket
            assert listener.getsockname() == addr
            with sockat.connect(addr) as client:
                accept_ping(listener, client)
            with sockat.connect_nonblocking(addr) as client:
                accept_ping(listener, client)
    over = b"\0" + b"x" * 108
    std = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with std, pytest.raises(OSError, match="AF_UNIX path too long"):
        std.bind(over)
    with pytest.raises(OSError, match="AF_UNIX path too long"):
        sockat.bind(over)


# ----- the returned sockets are the standard's -----


# sockat's sockets are set up like the standard's because they are made by
# the socket module (socket.socket(AF_UNIX, SOCK_STREAM)) on every route:
#
# * family AF_UNIX and type SOCK_STREAM; for a path the standard takes (60
#   bytes) a plain socket.socket, nothing added; for a longer one (300 bytes)
#   a PathSocket, a subclass of socket.socket (so isinstance holds and every
#   method is the standard's) that reports the path as the address, since the
#   kernel holds the spelling the route handed it instead;
# * close-on-exec (the socket module makes every socket non-inheritable),
#   checked on the fd itself (FD_CLOEXEC);
# * a listener and a `connect` stream are blocking, a `connect_nonblocking`
#   stream is not;
# * on macOS, no SO_NOSIGPIPE, as the socket module does not set it (Python
#   ignores SIGPIPE itself; Rust's std sets the option, which is why the
#   promise is "the same as the standard", checked against the standard's
#   own socket rather than a fixed answer).
@pytest.mark.parametrize("total", [60, 300])
@needs_long_bind
def test_the_sockets_are_set_up_as_the_standard_sets_them_up(tmp: TmpDirs, total: int) -> None:
    std = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    p = nest(tmp.new(), total)
    with std, sockat.bind(p) as listener, sockat.connect(p) as client, sockat.connect_nonblocking(p) as nb:
        for s in (listener, client, nb):
            assert isinstance(s, socket.socket)
            assert type(s) is (socket.socket if total <= DIRECT_MAX else PathSocket)
            assert (s.family, s.type) == (socket.AF_UNIX, socket.SOCK_STREAM)
            assert not s.get_inheritable()
            assert is_cloexec(s)
            if sys.platform == "darwin":
                assert s.getsockopt(socket.SOL_SOCKET, SO_NOSIGPIPE) == std.getsockopt(socket.SOL_SOCKET, SO_NOSIGPIPE)
                assert s.getsockopt(socket.SOL_SOCKET, SO_NOSIGPIPE) == 0
        assert listener.getblocking()
        assert client.getblocking()
        assert not nb.getblocking()
        assert client.gettimeout() is None
        assert nb.gettimeout() == 0.0


# The listener listens with the socket module's default backlog (`listen()`
# with no argument), as a standard listener does: as many nonblocking
# connects get into a sockat listener on a long path before it is full as into
# a standard listener on a short one. Counted against the standard rather
# than a number, since the default and how the kernel rounds it differ by
# system. Each queued connect holds an fd, so the count runs in a child
# process that raises its own limit of open files (_children.default_backlog).
@needs_long_bind
def test_the_listener_has_the_standards_default_backlog(tmp: TmpDirs) -> None:
    t = tmp.new()
    got = run_child("default_backlog", t, args={"DIR": t})
    assert got["sockat"] == got["std"]
    assert int(got["std"]) > 1


# Short paths are the standard's business: sockat hands them over unchanged, so
# the sockets are plain socket.socket objects with nothing added (the accepted
# stream and a dup too), the addresses the sockets report (getsockname of the
# listener and of an accepted stream, getpeername of a client) are exactly what
# the standard reports for the same path, and a standard client reaches a
# sockat listener and the other way round.
def test_a_short_path_behaves_exactly_like_the_standard(tmp: TmpDirs) -> None:
    t = tmp.new()
    p = os.fsdecode(t + b"/s.sock")
    std_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    std_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with std_listener, std_client:
        std_listener.bind(p)
        std_listener.listen()
        std_client.connect(p)
        accepted, _ = std_listener.accept()
        with accepted:
            want = (std_listener.getsockname(), std_client.getpeername(), accepted.getsockname())
    os.unlink(p)
    with sockat.bind(p) as listener:
        assert type(listener) is socket.socket
        for connect in (sockat.connect, sockat.connect_nonblocking):
            with connect(p) as client:
                assert type(client) is socket.socket
                accepted, _ = listener.accept()
                with accepted, accepted.dup() as dup:
                    assert type(accepted) is socket.socket
                    assert type(dup) is socket.socket
                    assert (listener.getsockname(), client.getpeername(), accepted.getsockname()) == want
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.connect(p)
            accept_ping(listener, c)
    q = os.fsdecode(t + b"/q.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as std_l:
        std_l.bind(q)
        std_l.listen()
        with sockat.connect(q) as client:
            accept_ping(std_l, client)


# A long path is reported the way the standard reports a short one, so code
# that reads the address back (asyncio's server cleanup, logging, repr) sees
# the path the caller gave and not the spelling a route handed to the kernel
# (`R/<name>` or /proc/self/fd/<n>/R/<name> for a bind, through a symlink R
# gone by then; /proc/self/fd/<n>/<name>, a relative path, a temporary
# symlink for a connect): the path, as a str decoded by os.fsdecode as the
# socket module decodes a pathname address (a bytes or os.PathLike path
# included), as the listener's and the accepted stream's own address
# (getsockname) and as a client's peer (getpeername), for both connects. What
# the standard reports from the kernel stays the kernel's: a client's own
# address and the accepted stream's peer (the client is not bound) are what
# the standard's are for a short path. Lengths on both sides of what a sockaddr
# carries (DIRECT_MAX + 1; 253 / 254 around the widened form of macOS) and far
# past it.
@pytest.mark.parametrize("total", [DIRECT_MAX + 1, 253, 254, 400, 1000])
@needs_long_bind
def test_a_long_path_reports_the_path_given_as_the_standard_reports_a_short_one(tmp: TmpDirs, total: int) -> None:
    t = tmp.new()
    short = os.fsdecode(t + b"/std.sock")
    with sockat.bind(short) as std_listener, sockat.connect(short) as std_client:
        std_accepted, _ = std_listener.accept()
        with std_accepted:
            unbound = (std_client.getsockname(), std_accepted.getpeername())
    makers: list[Callable[[bytes], str | bytes | os.PathLike[str] | os.PathLike[bytes]]] = [
        os.fsdecode,
        bytes,
        lambda b: Path(os.fsdecode(b)),
        BytesPathLike,
    ]
    for i, make in enumerate(makers):
        p = nest(t, total, f"{i}.sock".encode())
        want = os.fsdecode(p)
        with sockat.bind(make(p)) as listener:
            assert listener.getsockname() == want, make
            for connect in (sockat.connect, sockat.connect_nonblocking):
                with connect(make(p)) as client:
                    assert client.getpeername() == want, (make, connect)
                    accepted, _ = listener.accept()
                    with accepted:
                        assert accepted.getsockname() == want, (make, connect)
                        assert (client.getsockname(), accepted.getpeername()) == unbound
                        ping(accepted, client)


# A path is bytes to the kernel and need not be valid UTF-8 (Linux file systems
# take any byte but NUL and "/"). The socket module decodes a pathname address
# with os.fsdecode (surrogateescape), so a short path with the byte 0xff comes
# back as a str that os.fsencode turns into the same bytes again; a long path
# is reported the same way, round-tripping to the path given. macOS file
# systems refuse names that are not UTF-8, so this is Linux's.
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="macOS file systems take UTF-8 names only")
@needs_long_bind
def test_a_path_that_is_not_utf8_is_reported_as_the_socket_module_decodes_it(tmp: TmpDirs) -> None:
    t = tmp.new()
    for total in (60, 300):
        p = nest(t, total, b"\xff" + f"{total}.sock".encode())
        with sockat.bind(p) as listener, sockat.connect(p) as client:
            assert os.fsencode(listener.getsockname()) == p
            assert listener.getsockname() == os.fsdecode(p)
            assert os.fsencode(client.getpeername()) == p
            accept_ping(listener, client)


# The returned sockets are ordinary socket.socket objects (for a long path a
# PathSocket, a subclass that only reports the path as the address): a
# long-path listener accepts a client from another thread, and the streams can
# be wrapped in files, half-closed and duplicated as usual. A dup is the same
# kind of socket and reports the same path. Nothing about the route leaks into
# the objects.
@needs_long_bind
def test_the_results_are_ordinary_sockets(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 500)
    listener = sockat.bind(p)
    got: list[bytes] = []

    def client_side() -> None:
        with sockat.connect(p) as c:
            c.sendall(b"hello")
            c.shutdown(socket.SHUT_WR)
            with c.makefile("rb") as f:
                got.append(f.read())

    with listener:
        th = threading.Thread(target=client_side)
        th.start()
        server, _ = listener.accept()
        with server:
            dup = server.dup()
            with dup:
                assert type(dup) is PathSocket
                assert dup.getsockname() == os.fsdecode(p)
                assert recv_exact(dup, 5) == b"hello"
                assert dup.recv(1) == b""
            server.sendall(b"bye")
        th.join()
    assert got == [b"bye"]


# The socket's directory needs no more permission for a long path than the
# standard's bind / connect of a short path asks: search and write, not read.
# A directory of mode 0300 (write + search, no read) works at any length.
# Root bypasses permission checks, so as root (docker) the test cannot tell
# anything and is skipped. FreeBSD's bindat opens the directory O_RDONLY (the
# SPEC's way there), which needs read permission, so it is not promised there.
@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the permission under test")
@pytest.mark.skipif(sys.platform.startswith("freebsd"), reason="bindat opens the directory O_RDONLY")
@needs_long_bind
def test_a_directory_without_read_permission_is_enough(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 400)
    directory = parent(p)
    os.chmod(directory, 0o300)
    with pytest.raises(PermissionError):
        os.listdir(directory)
    with sockat.bind(p) as listener:
        assert stat.S_ISSOCK(os.lstat(p).st_mode)
        with sockat.connect(p) as client:
            accept_ping(listener, client)


# Many threads binding and connecting long paths at once, while other threads
# keep checking the cwd: no call may change the cwd of the process (other
# threads' relative lookups would break), and every socket lands at its own
# path. The lengths mix the routes the system picks.
@needs_long_bind
def test_concurrent_calls_leave_the_cwd_alone(tmp: TmpDirs) -> None:
    cwd, cwd_id = os.getcwd(), dir_id(".")
    stop = threading.Event()
    checks = [0]
    errors: list[BaseException] = []

    def watch() -> None:
        while not stop.is_set():
            if os.getcwd() != cwd or dir_id(".") != cwd_id:
                errors.append(AssertionError("the cwd changed"))
                return
            checks[0] += 1

    def work(i: int) -> None:
        try:
            t = tmp.new()
            for round_ in range(20):
                total = [120, 250, 400, 900][(i + round_) % 4]
                p = nest(t, total, f"{round_}.sock".encode())
                with sockat.bind(p) as listener:
                    assert is_socket(p)
                    with sockat.connect(p) as client:
                        server, _ = listener.accept()
                        with server:
                            ping(server, client)
        except BaseException as e:
            errors.append(e)

    watchers = [threading.Thread(target=watch) for _ in range(2)]
    workers = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for th in watchers + workers:
        th.start()
    for th in workers:
        th.join()
    stop.set()
    for th in watchers:
        th.join()
    assert errors == []
    assert checks[0] > 0
    assert os.getcwd() == cwd
