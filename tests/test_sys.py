"""The boundary to the C library (``_sys``): the lengths that decide which
path goes where, the hand-built ``sockaddr_un``, and the per-thread cwd."""

from __future__ import annotations

import errno
import os
import socket
import struct
import sys
import threading

import pytest
from _support import TmpDirs, accept_ping, dir_id, is_socket, nest, thread_cwd_usable

from sockat import _sys
from sockat._sys import DIRECT_MAX, SUN_PATH_CAP, WIDE_PATH_MAX, Op, op, sockaddr

BSD_LAYOUT = sys.platform.startswith(("darwin", "freebsd", "netbsd", "openbsd", "dragonfly"))
"""The systems whose ``sockaddr_un`` starts with a 1-byte ``sun_len``."""


# The longest path handed to the socket module is sun_path less the NUL: 107
# on Linux and illumos (sun_path is 108), 103 on macOS and the BSDs (sun_path
# is 104). The socket module itself refuses `len >= sizeof(sun_path)` (checked
# against the module in test_public), so these are its limits, written per
# system.
def test_the_lengths_per_system() -> None:
    if BSD_LAYOUT:
        assert (SUN_PATH_CAP, DIRECT_MAX) == (104, 103)
    else:
        assert (SUN_PATH_CAP, DIRECT_MAX) == (108, 107)
    # XNU takes an address of up to SOCK_MAXADDRLEN (255) bytes: 2 header
    # bytes and 253 bytes of path.
    assert WIDE_PATH_MAX == 253
    # The thread's own cwd (a means of the central route) exists where a
    # thread can have a cwd of its own.
    assert _sys.HAS_THREAD_CWD is (sys.platform.startswith("linux") or sys.platform == "darwin")


# The standard sockaddr_un built by hand holds the path and a terminating NUL,
# as the socket module builds it, with the system's header: on the BSDs and
# Apple a 1-byte sun_len (the address length) then a 1-byte family, elsewhere
# a 2-byte family in native byte order. The longest path is sun_path less the
# NUL; one byte more is ENAMETOOLONG (the kernel's error for a path too long
# for sun_path). Empty and NUL-containing paths are argument errors.
def test_the_standard_sockaddr_layout() -> None:
    a = sockaddr(b"ab")
    assert a[2:] == b"ab\0"
    if BSD_LAYOUT:
        assert a[:2] == bytes([len(a), socket.AF_UNIX])
    else:
        assert a[:2] == struct.pack("=H", socket.AF_UNIX)
    assert len(sockaddr(b"x" * DIRECT_MAX)) == 2 + DIRECT_MAX + 1
    with pytest.raises(OSError, match="too long") as e:
        sockaddr(b"x" * (DIRECT_MAX + 1))
    assert e.value.errno == errno.ENAMETOOLONG
    with pytest.raises(ValueError, match="empty"):
        sockaddr(b"")
    with pytest.raises(ValueError, match="NUL"):
        sockaddr(b"a\0b")


# The hand-built standard sockaddr is one the kernel takes as the socket
# module's own: bind(2) / connect(2) through `op` with it binds at the path
# (the address the kernel reports is the path, as for the socket module) and
# connects to it. This is the form bindat / connectat use.
def test_op_binds_and_connects_with_a_hand_built_sockaddr(tmp: TmpDirs) -> None:
    p = tmp.new() + b"/s.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with listener:
        op(Op.BIND, listener, sockaddr(p))
        listener.listen()
        assert is_socket(p)
        assert os.fsencode(listener.getsockname()) == p
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with client:
            op(Op.CONNECT, client, sockaddr(p))
            accept_ping(listener, client)


# A failure of the call comes back as the OSError subclass of its errno, as
# the socket module raises them: ENOENT for a missing socket is
# FileNotFoundError.
def test_op_raises_the_errno_of_the_call(tmp: TmpDirs) -> None:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with client, pytest.raises(FileNotFoundError):
        op(Op.CONNECT, client, sockaddr(tmp.new() + b"/missing.sock"))


# The widened sockaddr exists on Apple only: sun_len and the address length
# are both 2 + the path's length, with no NUL, so a path of up to 253 bytes
# fits; 254 is ENAMETOOLONG before the kernel is asked. Elsewhere asking for it
# is a programming error.
@pytest.mark.skipif(sys.platform != "darwin", reason="the widened sockaddr is Apple's")
def test_the_widened_sockaddr_layout() -> None:
    a = sockaddr(b"x" * 253, wide=True)
    assert a == bytes([2 + 253, socket.AF_UNIX]) + b"x" * 253
    with pytest.raises(OSError, match="too long"):
        sockaddr(b"x" * 254, wide=True)


@pytest.mark.skipif(sys.platform == "darwin", reason="the widened sockaddr is Apple's")
def test_the_widened_sockaddr_is_not_there_elsewhere() -> None:
    with pytest.raises(AssertionError):
        sockaddr(b"x", wide=True)


# A thread entered into a directory has that directory as its own cwd (what
# the central route binds / connects `R/<name>` against), while the
# process cwd, which every other thread keeps using, does not change. Where
# the system will not give the thread a cwd of its own (Linux under a seccomp
# filter such as docker's default, where unshare(CLONE_FS) is not even tried),
# entering answers False and nothing changes.
@pytest.mark.skipif(not _sys.HAS_THREAD_CWD, reason="no thread cwd on this system")
def test_a_thread_gets_a_cwd_of_its_own(tmp: TmpDirs) -> None:
    target = os.path.dirname(nest(tmp.new(), 100, b"x"))
    fd = os.open(target, os.O_RDONLY)
    process_cwd = dir_id(".")
    seen: dict[str, object] = {}

    def in_thread() -> None:
        entered = _sys.enter_dir_in_this_thread(fd)
        seen["entered"] = entered
        seen["inside"] = dir_id(".")

    try:
        th = threading.Thread(target=in_thread)
        th.start()
        th.join()
    finally:
        os.close(fd)
    assert dir_id(".") == process_cwd
    assert seen["entered"] is thread_cwd_usable()
    if thread_cwd_usable():
        assert seen["inside"] == dir_id(target)
    else:
        assert seen["inside"] == process_cwd
