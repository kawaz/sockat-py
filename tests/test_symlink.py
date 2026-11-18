"""The symlink route of the POSIX order (every system but FreeBSD; on Linux
when the /proc route cannot be used): the places for the symlink, its name,
and how it is created and removed.

Unless a test says otherwise the thread-cwd route is off
(:func:`_support.posix_cfg`), so a path that no symlink reaches ends at the
fork route's place, where this implementation answers ``ENAMETOOLONG``
(there is no fork route in Python), which keeps "the symlink route was not
used" observable."""

from __future__ import annotations

import errno
import os
import stat
import sys

import pytest
from _support import (
    NO_PLACE,
    TmpDirs,
    accept_ping,
    assert_empty,
    assert_far_from_cwd,
    assert_untouched,
    check_unreachable,
    is_socket,
    listener_at,
    mark_untouched,
    nest,
    parent,
    place,
    posix_cfg,
    raises_errno,
    run_child,
    stale_socket,
    with_name,
)

from sockat import _route
from sockat._route import BIND, CONNECT, CONNECT_NONBLOCKING, LINK_PREFIX, LINK_RANDOM_LEN, Config, Route, TempLink
from sockat._sys import DIRECT_MAX, IS_APPLE

pytestmark = pytest.mark.skipif(sys.platform.startswith("freebsd"), reason="FreeBSD takes bindat only")


def long_dir(tmp: TmpDirs, total: int) -> bytes:
    """A directory whose path is exactly ``total`` bytes, for a place for
    the symlink too long for a spelling through it."""
    d = nest(tmp.new(), total, b"base")
    os.mkdir(d)
    return d


# ----- the name -----


# The symlink's name is "sockat." and 10 letters or digits drawn from the
# system's secure generator (the secrets module): a link left behind by a
# process that died is recognisable as sockat's, and a name another user could
# predict (and pre-create) is not used. Two draws differ: a generator that
# returned the same name each time would make every name collide.
def test_the_name_is_sockat_dot_and_ten_random_letters_or_digits() -> None:
    names = {_route.random_name() for _ in range(50)}
    assert len(names) == 50
    for n in names:
        assert n.startswith(LINK_PREFIX)
        assert len(n) == len(LINK_PREFIX) + LINK_RANDOM_LEN == len(b"sockat.") + 10
        assert n.isascii()
        assert n[len(LINK_PREFIX) :].isalnum()


# Two links made back to back are distinct, each points to the target as given,
# an existing file under the place is never touched, and leaving the `with`
# block removes only its own link.
def test_temporary_links_are_unique_and_removed_at_the_end(tmp: TmpDirs) -> None:
    base = tmp.new()
    target = tmp.new()
    with open(base + b"/keep", "wb") as f:
        f.write(b"x")
    with TempLink.create(base, target) as a, TempLink.create(base, target) as b:
        assert a.path != b.path
        for link in (a.path, b.path):
            assert os.path.dirname(link) == base
            assert os.readlink(link) == target
    assert os.listdir(base) == [b"keep"]


# A name that is already taken (by anything: a file, another link) is skipped
# for the next name, and the existing entry is left as it was: symlink(2) fails
# with EEXIST rather than replacing it (the O_EXCL of symlinks). The names come
# from a stand-in generator here so the collision is certain.
def test_a_taken_name_is_skipped_for_the_next_one(tmp: TmpDirs) -> None:
    base = tmp.new()
    target = tmp.new()
    taken = LINK_PREFIX + b"A" * LINK_RANDOM_LEN
    free = LINK_PREFIX + b"B" * LINK_RANDOM_LEN
    with open(base + b"/" + taken, "wb") as f:
        f.write(b"keep")
    names = iter([taken, free])
    calls = [0]

    def next_name() -> bytes:
        calls[0] += 1
        return next(names)

    with TempLink.create(base, target, next_name) as link:
        assert calls[0] == 2
        assert link.path == base + b"/" + free
        assert os.readlink(link.path) == target
    with open(base + b"/" + taken, "rb") as kept:
        assert kept.read() == b"keep"
    assert not os.path.lexists(base + b"/" + free)


# When every name tried is taken, the attempt ends after 16 tries with
# FileExistsError (the place then counts as unusable and the next is tried); it
# does not loop forever and touches nothing.
def test_running_out_of_names_ends_after_16_tries(tmp: TmpDirs) -> None:
    base = tmp.new()
    target = tmp.new()
    taken = LINK_PREFIX + b"A" * LINK_RANDOM_LEN
    with open(base + b"/" + taken, "wb") as f:
        f.write(b"keep")
    calls = [0]

    def next_name() -> bytes:
        calls[0] += 1
        return taken

    with pytest.raises(FileExistsError):
        TempLink.create(base, target, next_name)
    assert calls[0] == TempLink.ATTEMPTS == 16
    assert os.listdir(base) == [taken]


# Through the route: with every name colliding under the first place, it
# counts as unusable after 16 tries (nothing was created in it: its mtime is
# unchanged, as a failed symlink(2) leaves it) and the symlink goes under the
# next place.
def test_a_place_whose_names_run_out_is_passed_over(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    p = nest(tmp.new(), 600)
    crowded = tmp.new()
    free = tmp.new()
    taken = LINK_PREFIX + b"A" * LINK_RANDOM_LEN
    os.symlink(b"/nonexistent", crowded + b"/" + taken)
    mark_untouched(crowded)
    real_create = TempLink.create

    def create(base: bytes, target: bytes) -> TempLink:
        # Every name collides under the crowded place; elsewhere the names
        # are the real random ones.
        def names() -> bytes:
            return taken if base == crowded else _route.random_name()

        return real_create(base, target, names)

    monkeypatch.setattr(TempLink, "create", staticmethod(create))
    listener, r = _route.run_long(BIND, p, posix_cfg([crowded, free]))
    with listener:
        assert r is Route.SYMLINK_TO_DIR
        assert is_socket(p)
    assert_untouched(crowded)
    assert os.listdir(crowded) == [taken]
    assert_empty(free)


# ----- the places -----


# The places tried for the symlink are TMPDIR, then /tmp: a TMPDIR too long to
# shorten anything must not end the symlink route while /tmp is there. An unset
# or empty TMPDIR leaves /tmp alone. A relative TMPDIR would make the link's
# location depend on the cwd, so it is left out, but /tmp is still tried. A
# TMPDIR that is /tmp (written with or without the trailing slash) is tried
# once, not twice. The rest of the configuration is what the system has: the
# widened sockaddr on Apple, /proc/self/fd, and the thread-cwd route where it
# exists.
def test_the_places_are_tmpdir_then_tmp() -> None:
    def bases(tmpdir: bytes | None) -> tuple[bytes, ...]:
        return Config.from_tmpdir(tmpdir).tmp_bases

    assert bases(None) == (b"/tmp",)
    assert bases(b"") == (b"/tmp",)
    assert bases(b"/var/x/") == (b"/var/x/", b"/tmp")
    assert bases(b"rel") == (b"/tmp",)
    assert bases(b"/tmp") == (b"/tmp",)
    assert bases(b"/tmp/") == (b"/tmp",)
    assert bases(b"//tmp/.") == (b"/tmp",)
    c = Config.from_tmpdir(None)
    assert c.wide is IS_APPLE
    assert c.proc_self_fd == b"/proc/self/fd"
    assert c.thread_cwd is (sys.platform.startswith("linux") or sys.platform == "darwin")


# A place is used only where no other user can replace the symlink between its
# creation and the bind / connect through it. The rule on a directory's mode
# and owner, row by row (owner and user as uids; 0 is root):
#
# * sticky and owned by root (as /tmp is) or by the user: yes. Sticky and owned
#   by another user: no, the directory's owner can remove or rename any entry;
# * the user's own, without group / other write (0700, 0755): yes. With either
#   (0775, 0757, 0777) and not sticky: no;
# * another user's without group / other write: no (it is theirs to change);
#   root's, not sticky: no (except for root itself).
@pytest.mark.parametrize(
    ("mode", "owner", "want"),
    [
        (0o1777, 0, True),
        (0o1777, 1000, True),
        (0o1700, 1000, True),
        (0o1777, 2000, False),
        (0o700, 1000, True),
        (0o755, 1000, True),
        (0o775, 1000, False),
        (0o757, 1000, False),
        (0o777, 1000, False),
        (0o700, 2000, False),
        (0o700, 0, False),
    ],
)
def test_a_place_must_be_sticky_or_the_users_own(mode: int, owner: int, want: bool) -> None:
    assert _route.only_we_can_replace(mode | stat.S_IFDIR, owner, 1000) is want


def test_roots_own_place_is_safe_for_root() -> None:
    assert _route.only_we_can_replace(0o700 | stat.S_IFDIR, 0, 0)


# A place must be a directory to be safe: a file of the user's own with a mode
# that would make a directory safe (0644, 0700) is not one, nor is a missing
# place. Taking a file for a safe place would send a connect on Linux past the
# fd-held form, which uses only places that are not safe.
def test_a_place_that_is_not_a_directory_is_not_safe(tmp: TmpDirs) -> None:
    t = tmp.new()
    for mode in (0o644, 0o700):
        f = t + f"/file{mode:o}".encode()
        with open(f, "wb"):
            pass
        os.chmod(f, mode)
        assert not _route.safe_base(f), oct(mode)
    assert not _route.safe_base(t + b"/missing")
    assert _route.safe_base(t)


# The same rule on real directories, through the route order. A place anyone may
# write to and that is not sticky (0777, 0775, 0757) is passed over without
# creating anything in it (its mtime is unchanged) and the next place is used; a
# sticky place (1777) and the user's own (0700, 0755) are used and left empty.
# With only unsafe places there is no symlink route: the path ends at the fork
# route's place (ENAMETOOLONG) with nothing created.
def test_an_unsafe_place_is_passed_over(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 600)
    assert_far_from_cwd(p)
    places = tmp.new()
    good = tmp.new()
    unsafe_modes = (0o777, 0o775, 0o757)
    for mode in unsafe_modes:
        bad = place(places, f"bad{mode:o}".encode(), mode)
        mark_untouched(bad)
        q = with_name(p, f"{mode:o}.sock".encode())
        c = posix_cfg([bad, good])
        listener, r = _route.run_long(BIND, q, c)
        with listener:
            assert r is Route.SYMLINK_TO_DIR, oct(mode)
            assert is_socket(q)
            client, r = _route.run_long(CONNECT, q, c)
            with client:
                assert r is Route.SYMLINK_TO_FILE, oct(mode)
                accept_ping(listener, client)
        assert_untouched(bad)
        assert_empty(good)
    for mode in (0o1777, 0o700, 0o755):
        ok = place(places, f"ok{mode:o}".encode(), mode)
        q = with_name(p, f"ok{mode:o}.sock".encode())
        c = posix_cfg([ok])
        listener, r = _route.run_long(BIND, q, c)
        with listener:
            assert r is Route.SYMLINK_TO_DIR, oct(mode)
            client, r = _route.run_long(CONNECT, q, c)
            with client:
                assert r is Route.SYMLINK_TO_FILE, oct(mode)
                accept_ping(listener, client)
        assert_empty(ok)

    bad_places = [places + f"/bad{m:o}".encode() for m in unsafe_modes]
    for b in bad_places:
        mark_untouched(b)
    check_unreachable(with_name(p, b"none.sock"), posix_cfg(bad_places), bad_places)


# Run as root (docker), a place owned by another user is constructed too
# (chown) and must be passed over, sticky or not: its owner could replace the
# symlink. Other users cannot chown, so this is skipped for them.
@pytest.mark.skipif(os.geteuid() != 0, reason="only root can make a place owned by another user")
def test_another_users_place_is_passed_over(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 600)
    places = tmp.new()
    good = tmp.new()
    for mode in (0o1777, 0o700):
        theirs = place(places, f"theirs{mode:o}".encode(), mode)
        os.chown(theirs, 65534, -1)
        mark_untouched(theirs)
        q = with_name(p, f"theirs{mode:o}.sock".encode())
        listener, r = _route.run_long(BIND, q, posix_cfg([theirs, good]))
        listener.close()
        assert r is Route.SYMLINK_TO_DIR, oct(mode)
        assert_untouched(theirs)


# A path far from the cwd (600 bytes under /tmp, so no relative spelling fits)
# goes through a symlink `<place>/sockat.XXXXXXXXXX`: a bind through one to the
# socket's directory, so the socket is created in the real directory; a connect
# (blocking or not) through one to the socket file. With a short place both
# spellings fit the plain sockaddr. The place is empty afterwards: the link is
# removed once the call returns.
def test_a_path_far_from_the_cwd_goes_through_a_temporary_symlink(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 600)
    assert_far_from_cwd(p)
    base = tmp.new()
    c = posix_cfg([base])
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is Route.SYMLINK_TO_DIR
        assert is_socket(p)
        for req in (CONNECT, CONNECT_NONBLOCKING):
            client, r = _route.run_long(req, p, c)
            with client:
                assert r is Route.SYMLINK_TO_FILE
                accept_ping(listener, client)
    assert_empty(base)


# The link to the socket file is spelled `<place>/sockat.XXXXXXXXXX` whatever
# the file name, so a connect reaches a name too long for the link to the
# directory (`<place>/sockat.XXXXXXXXXX/<name>` over DIRECT_MAX bytes; a bind
# never takes the widened form, Apple included), which no bind route reaches.
# On Apple it is also run with the widened form on (a name of 240 bytes): the
# bind is still ENAMETOOLONG, and the connect still takes the link to the file
# in the plain sockaddr (the path, 600 bytes, is too long for the widened form
# itself). The socket is made under a short name and renamed.
def test_a_connect_reaches_a_file_name_too_long_for_the_link_to_the_directory(tmp: TmpDirs) -> None:
    t = tmp.new()
    base = tmp.new()
    cases = [(posix_cfg([base], wide=False), 90)]
    if IS_APPLE:
        cases.append((posix_cfg([base]), 240))
    for c, n in cases:
        p = nest(t, 600, b"n" * n)
        with raises_errno(errno.ENAMETOOLONG):
            _route.run_long(BIND, p, c)
        with listener_at(p) as listener:
            for req in (CONNECT, CONNECT_NONBLOCKING):
                client, r = _route.run_long(req, p, c)
                with client:
                    assert r is Route.SYMLINK_TO_FILE, (n, req)
                    accept_ping(listener, client)
    assert_empty(base)


# Both links hold the path as the caller wrote it, so the kernel resolves them
# as it resolves the path: a `..` right after a symlink climbs out of the
# symlink's target, not out of the directory the symlink sits in. With
# `<t>/link -> <t>/real/sub`, `<t>/link/../x/...` names `<t>/real/x/...`; a
# socket at `<t>/x/...` (where a lexical `..` would lead) is a decoy that must
# not be reached. The bind goes through the link to the directory, the connect
# through the link to the file.
def test_the_links_resolve_the_path_as_the_kernel_does(tmp: TmpDirs) -> None:
    t = tmp.new()
    base = tmp.new()
    real = t + b"/real"
    os.makedirs(real + b"/sub")
    os.symlink(real + b"/sub", t + b"/link")
    at_real = nest(real + b"/x", len(real) + 3 + 500)
    below = at_real[len(real) + 1 :]
    spelled = t + b"/link/../" + below
    decoy = t + b"/" + below
    os.makedirs(parent(decoy))
    c = posix_cfg([base])

    listener, r = _route.run_long(BIND, spelled, c)
    with listener:
        assert r is Route.SYMLINK_TO_DIR
        assert is_socket(at_real)
        assert not is_socket(decoy)
        decoy_listener, _ = _route.run(BIND, decoy)
        with decoy_listener:
            decoy_listener.setblocking(False)
            client, r = _route.run_long(CONNECT, spelled, c)
            with client:
                assert r is Route.SYMLINK_TO_FILE
                accept_ping(listener, client)
            with pytest.raises(BlockingIOError):
                decoy_listener.accept()
    assert_empty(base)


# The symlink route checks the length through the link before creating
# anything: a place so long that no sockaddr carries even
# `<place>/sockat.XXXXXXXXXX` (the spelling of the link to the file; on Apple,
# not even the widened form) is skipped without touching it, for bind and
# connect alike.
def test_a_place_too_long_for_the_symlink_route_is_not_touched(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 600)
    base = long_dir(tmp, 250)
    mark_untouched(base)
    check_unreachable(p, posix_cfg([base]), [base])


# In the plain sockaddr (every system without the widened form, and Apple when
# the kernel refuses it, stood in for by wide=False), a path of 200 bytes far
# from the cwd goes through the symlink under a short place; with a long place
# (150 bytes: the spelling through it would need the widened form), a place
# that does not exist, or no place at all, there is no symlink route.
def test_in_the_plain_sockaddr_a_long_place_leaves_no_symlink_route(tmp: TmpDirs) -> None:
    t = tmp.new()
    short_base = tmp.new()
    long_base = long_dir(tmp, 150)
    p = nest(t, 200, b"0.sock")
    c = posix_cfg([short_base], wide=False)
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is Route.SYMLINK_TO_DIR
        client, r = _route.run_long(CONNECT, p, c)
        with client:
            assert r is Route.SYMLINK_TO_FILE
            accept_ping(listener, client)
    assert_empty(short_base)
    for i, bases in enumerate(([long_base], [NO_PLACE], [])):
        mark_untouched(long_base)
        q = with_name(p, f"{i + 1}.sock".encode())
        check_unreachable(q, posix_cfg(bases, wide=False), [long_base])


# A failure through the symlink is the kernel's error for the target (bind
# through the link to the directory: EADDRINUSE for an existing socket, ENOENT
# for a missing directory, since a link to a missing directory dangles; connect
# through the link to the file: ENOENT for a missing socket, ECONNREFUSED for a
# socket nobody listens on), and the link is removed in the failure case too.
def test_failures_through_the_symlink_are_the_kernels_and_cleaned_up(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 600)
    base = tmp.new()
    c = posix_cfg([base])
    listener, _ = _route.run_long(BIND, p, c)
    with listener, raises_errno(errno.EADDRINUSE):
        _route.run_long(BIND, p, c)
    missing = parent(p) + b"/nope/s.sock"
    with pytest.raises(FileNotFoundError):
        _route.run_long(BIND, missing, c)
    with pytest.raises(FileNotFoundError):
        _route.run_long(CONNECT, with_name(p, b"x.sock"), c)
    with pytest.raises(FileNotFoundError):
        _route.run_long(CONNECT, missing, c)
    stale = with_name(p, b"stale.sock")
    stale_socket(stale)
    with pytest.raises(ConnectionRefusedError):
        _route.run_long(CONNECT, stale, c)
    assert_empty(base)


# A place where the symlink cannot be created (missing, or a file rather than a
# directory) makes the symlink route unusable there rather than an error, for
# the link to the directory (bind) and to the file (connect), and nothing is
# created anywhere.
def test_a_place_where_the_link_cannot_be_created_is_unusable(tmp: TmpDirs) -> None:
    t = tmp.new()
    p = nest(t, 600)
    directory, name = os.path.split(p)
    file = t + b"/file"
    with open(file, "wb"):
        pass
    for base in (NO_PLACE, file):
        for req in (BIND, CONNECT):
            out = _route.symlink_route(req, base, p, directory, name, DIRECT_MAX)
            assert out is _route.Skip.UNUSABLE, (base, req)
    assert not is_socket(p)


# Through the route order: a place where the link cannot be created is passed
# over and the next place is used, the socket at the given path. With every
# place unusable there is no symlink route.
def test_a_place_where_the_link_cannot_be_created_moves_on_to_the_next(tmp: TmpDirs) -> None:
    t = tmp.new()
    p = nest(t, 600)
    file = t + b"/file"
    with open(file, "wb"):
        pass
    good = tmp.new()
    for i, bad in enumerate((NO_PLACE, file)):
        q = with_name(p, f"{i}.sock".encode())
        c = posix_cfg([bad, good])
        listener, r = _route.run_long(BIND, q, c)
        with listener:
            assert r is Route.SYMLINK_TO_DIR, bad
            assert is_socket(q)
            client, r = _route.run_long(CONNECT, q, c)
            with client:
                assert r is Route.SYMLINK_TO_FILE
                accept_ping(listener, client)
    assert_empty(good)
    check_unreachable(with_name(p, b"none.sock"), posix_cfg([NO_PLACE, file]))


# The symlink goes under the first place where the spelling through it fits.
# A place so long that even the widened form cannot carry it (a deeply nested
# TMPDIR) is passed over without creating anything in it, and the next place
# is used: through a short place the spelling fits the plain sockaddr. With
# two short places the first one is used and the second is never touched.
def test_the_symlink_goes_under_the_first_place_where_the_path_fits(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 600)
    long_base = long_dir(tmp, 250)
    first = tmp.new()
    second = tmp.new()

    mark_untouched(long_base)
    c = posix_cfg([long_base, first])
    listener, r = _route.run_long(BIND, p, c)
    with listener:
        assert r is Route.SYMLINK_TO_DIR
        assert is_socket(p)
        client, r = _route.run_long(CONNECT, p, c)
        with client:
            assert r is Route.SYMLINK_TO_FILE
            accept_ping(listener, client)
    assert_untouched(long_base)
    assert_empty(first)

    mark_untouched(second)
    q = with_name(p, b"q.sock")
    c = posix_cfg([first, second])
    listener, r = _route.run_long(BIND, q, c)
    with listener:
        assert r is Route.SYMLINK_TO_DIR
        client, _ = _route.run_long(CONNECT, q, c)
        with client:
            accept_ping(listener, client)
    assert_untouched(second)
    assert_empty(first)


# A bind / connect that actually ran through the symlink under the first place
# is the result: its error is returned and the call is not made again under the
# next place (which would repeat it, or worse, succeed somewhere the caller did
# not mean), nor on the thread-cwd route after it (on here). The second place
# stays untouched (no link was even created there), for EADDRINUSE, a missing
# directory (ENOENT through a dangling link), a missing socket (ENOENT) and a
# socket nobody listens on (ECONNREFUSED).
def test_a_failure_through_the_symlink_is_not_retried(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 600)
    first = tmp.new()
    second = tmp.new()
    c = posix_cfg([first, second], thread_cwd=True)
    listener, r = _route.run_long(BIND, p, c)
    stale = with_name(p, b"stale.sock")
    stale_socket(stale)
    with listener:
        assert r is Route.SYMLINK_TO_DIR
        mark_untouched(second)
        before = sorted(os.listdir(parent(p)))
        with raises_errno(errno.EADDRINUSE):
            _route.run_long(BIND, p, c)
        with pytest.raises(FileNotFoundError):
            _route.run_long(BIND, parent(p) + b"/nope/s.sock", c)
        with pytest.raises(FileNotFoundError):
            _route.run_long(CONNECT, with_name(p, b"x.sock"), c)
        with pytest.raises(ConnectionRefusedError):
            _route.run_long(CONNECT, stale, c)
        assert sorted(os.listdir(parent(p))) == before
    assert_untouched(second)
    assert_empty(first)


# The same through the real environment (Config.from_env, as the public API
# reads it; on Linux with /proc made to look absent): with TMPDIR set to a
# directory too long for the symlink, a 600-byte path still goes through a
# symlink, under /tmp, and nothing is created in TMPDIR. Runs in a child
# process because it sets TMPDIR (the cwd is / so the relative spelling is
# long too).
def test_a_tmpdir_too_long_for_the_symlink_falls_back_to_tmp(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 600)
    long_tmpdir = long_dir(tmp, 250)
    mark_untouched(long_tmpdir)
    out = run_child("bind_from_env", b"/", {"PATH": p}, env={"TMPDIR": long_tmpdir})
    assert out["route"] == Route.SYMLINK_TO_DIR.value, out
    assert out["bases"] == f'["{os.fsdecode(long_tmpdir)}", "/tmp"]', out
    assert is_socket(p)
    assert_untouched(long_tmpdir)


# The link's target is the caller's own spelling made absolute against the
# cwd, so a long path given relative to the cwd lands in the directory it
# names below the cwd. Runs in a child process with the cwd at a temporary
# directory: the relative spelling is longer than the plain sockaddr, so no
# relative route; the symlink under a short place carries it.
def test_a_long_relative_path_goes_through_a_link_to_its_absolute_target(tmp: TmpDirs) -> None:
    t = tmp.new()
    base = tmp.new()
    p = nest(t, len(t) + 1 + 300)
    rel = p[len(t) + 1 :]
    out = run_child("relative_input", t, {"REL": rel, "BASE": base})
    assert out == {"bind": Route.SYMLINK_TO_DIR.value, "connect": Route.SYMLINK_TO_FILE.value}
    assert is_socket(p)
    assert_empty(base)
