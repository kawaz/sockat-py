"""The symlink fallback of a connect (every system but FreeBSD; on Linux
after /proc and the proc-held route, so /proc is made to look absent here): the
places for the symlink, its name, and how it is created and removed. A bind
never takes it (the routes through D carry the binds).

Unless a test says otherwise there is no place for D and the thread's own
cwd is off (:func:`_support.fallback_cfg`), so the routes through D are not used
and a connect that no symlink reaches is ``ENAMETOOLONG``. The listeners are
bound by the standard under a short name and renamed to the path
(:func:`_support.listener_at`), so they do not depend on any route."""

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
    fallback_cfg,
    listener_at,
    mark_untouched,
    needs_long_bind,
    nest,
    parent,
    place,
    raises_errno,
    run_child,
    stale_socket,
    with_name,
)

from sockat import _route
from sockat._route import ATTEMPTS, CONNECT, CONNECT_NONBLOCKING, OWN_DIR_PREFIX, RANDOM_LEN, Route, TempLink
from sockat._sys import DIRECT_MAX, IS_APPLE

pytestmark = pytest.mark.skipif(sys.platform.startswith("freebsd"), reason="FreeBSD's connect falls back to connectat")


def long_dir(tmp: TmpDirs, total: int) -> bytes:
    """A directory whose path is exactly ``total`` bytes, for a place for
    the symlink too long for a spelling through it."""
    d = nest(tmp.new(), total, b"base")
    os.mkdir(d)
    return d


def far(tmp: TmpDirs, name: bytes = b"s.sock") -> bytes:
    """A socket path 600 bytes deep under /tmp, far from the cwd (no relative
    spelling fits, not even the widened one)."""
    p = nest(tmp.new(), 600, name)
    assert_far_from_cwd(p)
    return p


def connects_through(p: bytes, cfg: _route.Config, want: Route = Route.SYMLINK) -> None:
    """A listener at ``p`` is reached by both connects through ``cfg``, by
    the route ``want``."""
    with listener_at(p) as listener:
        for req in (CONNECT, CONNECT_NONBLOCKING):
            client, r = _route.run_long(req, p, cfg)
            with client:
                assert r is want, (req, r)
                accept_ping(listener, client)


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
        assert n.startswith(OWN_DIR_PREFIX)
        assert len(n) == len(OWN_DIR_PREFIX) + RANDOM_LEN == len(b"sockat.") + 10
        assert n.isascii()
        assert n[len(OWN_DIR_PREFIX) :].isalnum()


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
    taken = OWN_DIR_PREFIX + b"A" * RANDOM_LEN
    free = OWN_DIR_PREFIX + b"B" * RANDOM_LEN
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
    taken = OWN_DIR_PREFIX + b"A" * RANDOM_LEN
    with open(base + b"/" + taken, "wb") as f:
        f.write(b"keep")
    calls = [0]

    def next_name() -> bytes:
        calls[0] += 1
        return taken

    with pytest.raises(FileExistsError):
        TempLink.create(base, target, next_name)
    assert calls[0] == ATTEMPTS == 16
    assert os.listdir(base) == [taken]


# Through the route: with every name colliding under the first place, it
# counts as unusable after 16 tries (nothing was created in it: its mtime is
# unchanged, as a failed symlink(2) leaves it) and the symlink goes under the
# next place.
def test_a_place_whose_names_run_out_is_passed_over(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    p = far(tmp)
    crowded = tmp.new()
    free = tmp.new()
    taken = OWN_DIR_PREFIX + b"A" * RANDOM_LEN
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
    connects_through(p, fallback_cfg([crowded, free]))
    assert_untouched(crowded)
    assert os.listdir(crowded) == [taken]
    assert_empty(free)


# ----- the places -----


# A place is used only where no other user can replace the symlink between its
# creation and the connect through it. The rule on a directory's mode and
# owner, row by row (owner and user as uids; 0 is root):
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
# place.
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
# With only unsafe places there is no symlink fallback: ENAMETOOLONG with
# nothing created.
def test_an_unsafe_place_is_passed_over(tmp: TmpDirs) -> None:
    p = far(tmp)
    places = tmp.new()
    good = tmp.new()
    unsafe_modes = (0o777, 0o775, 0o757)
    for mode in unsafe_modes:
        bad = place(places, f"bad{mode:o}".encode(), mode)
        mark_untouched(bad)
        connects_through(with_name(p, f"{mode:o}.sock".encode()), fallback_cfg([bad, good]))
        assert_untouched(bad)
        assert_empty(good)
    for mode in (0o1777, 0o700, 0o755):
        ok = place(places, f"ok{mode:o}".encode(), mode)
        connects_through(with_name(p, f"ok{mode:o}.sock".encode()), fallback_cfg([ok]))
        assert_empty(ok)

    bad_places = [places + f"/bad{m:o}".encode() for m in unsafe_modes]
    for b in bad_places:
        mark_untouched(b)
    check_unreachable(with_name(p, b"none.sock"), fallback_cfg(bad_places), bad_places)


# Run as root (docker), a place owned by another user is constructed too
# (chown) and must be passed over, sticky or not: its owner could replace the
# symlink. Other users cannot chown, so this is skipped for them.
@pytest.mark.skipif(os.geteuid() != 0, reason="only root can make a place owned by another user")
def test_another_users_place_is_passed_over(tmp: TmpDirs) -> None:
    p = far(tmp)
    places = tmp.new()
    good = tmp.new()
    for mode in (0o1777, 0o700):
        theirs = place(places, f"theirs{mode:o}".encode(), mode)
        os.chown(theirs, 65534, -1)
        mark_untouched(theirs)
        connects_through(with_name(p, f"theirs{mode:o}.sock".encode()), fallback_cfg([theirs, good]))
        assert_untouched(theirs)


# A path far from the cwd (600 bytes under /tmp, so no relative spelling fits)
# is connected to, blocking or not, through a symlink `<place>/sockat.XXXXXXXXXX`
# to the socket file. The place is empty afterwards: the link is removed once
# the call returns. The spelling does not hold the file name, so a name too
# long for any sockaddr but the widened one (90 bytes; 240 on Apple, where the
# path, 600 bytes, is still too long for the widened form itself) is reached
# the same way.
def test_a_path_far_from_the_cwd_is_connected_to_through_a_temporary_symlink(tmp: TmpDirs) -> None:
    t = tmp.new()
    base = tmp.new()
    cases = [(fallback_cfg([base], wide=False), b"s.sock"), (fallback_cfg([base], wide=False), b"n" * 90)]
    if IS_APPLE:
        cases.append((fallback_cfg([base]), b"m" * 240))
    for c, name in cases:
        connects_through(nest(t, 600, name), c)
    assert_empty(base)


# The link holds the path as the caller wrote it, so the kernel resolves it as
# it resolves the path: a `..` right after a symlink climbs out of the
# symlink's target, not out of the directory the symlink sits in. With
# `<t>/link -> <t>/real/sub`, `<t>/link/../x/...` names `<t>/real/x/...`; a
# listener at `<t>/x/...` (where a lexical `..` would lead) is a decoy that
# must not be reached.
def test_the_link_resolves_the_path_as_the_kernel_does(tmp: TmpDirs) -> None:
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
    with listener_at(at_real) as listener, listener_at(decoy) as decoy_listener:
        decoy_listener.setblocking(False)
        client, r = _route.run_long(CONNECT, spelled, fallback_cfg([base]))
        with client:
            assert r is Route.SYMLINK
            accept_ping(listener, client)
        with pytest.raises(BlockingIOError):
            decoy_listener.accept()
    assert_empty(base)


# The length through the link is checked before anything is created: a place
# so long that no sockaddr carries even `<place>/sockat.XXXXXXXXXX` (on Apple,
# not even the widened form) is skipped without touching it. In the plain
# sockaddr (wide off) a 150-byte place is such a place too, and so are a
# place that does not exist and no place at all.
def test_a_place_too_long_for_the_symlink_is_not_touched(tmp: TmpDirs) -> None:
    p = far(tmp)
    long250 = long_dir(tmp, 250)
    long150 = long_dir(tmp, 150)
    for d in (long250, long150):
        mark_untouched(d)
    check_unreachable(p, fallback_cfg([long250]), [long250])
    for i, bases in enumerate(([long150], [NO_PLACE], [])):
        check_unreachable(with_name(p, f"{i}.sock".encode()), fallback_cfg(bases, wide=False), [long150])


# A failure through the symlink is the kernel's error for the target (ENOENT
# for a missing socket and for a missing directory, ECONNREFUSED for a socket
# nobody listens on), and the link is removed in the failure case too. The
# call is not made again under the next place (which stays untouched).
def test_a_failure_through_the_symlink_is_the_kernels_and_not_retried(tmp: TmpDirs) -> None:
    p = far(tmp)
    first = tmp.new()
    second = tmp.new()
    c = fallback_cfg([first, second])
    stale = with_name(p, b"stale.sock")
    stale_socket(stale)
    mark_untouched(second)
    for req in (CONNECT, CONNECT_NONBLOCKING):
        with pytest.raises(FileNotFoundError):
            _route.run_long(req, p, c)
        with pytest.raises(FileNotFoundError):
            _route.run_long(req, parent(p) + b"/nope/s.sock", c)
        with pytest.raises(ConnectionRefusedError):
            _route.run_long(req, stale, c)
    assert_empty(first)
    assert_untouched(second)


# A place where the symlink cannot be created (missing, or a file rather than a
# directory) makes the symlink fallback unusable there rather than an error;
# through the route order the next place is used, and with every place
# unusable the connect is ENAMETOOLONG.
def test_a_place_where_the_link_cannot_be_created_moves_on_to_the_next(tmp: TmpDirs) -> None:
    t = tmp.new()
    p = far(tmp)
    file = t + b"/file"
    with open(file, "wb"):
        pass
    for base in (NO_PLACE, file):
        assert _route.symlink_route(CONNECT, base, p, DIRECT_MAX) is _route.Skip.UNUSABLE, base
    good = tmp.new()
    for i, bad in enumerate((NO_PLACE, file)):
        connects_through(with_name(p, f"{i}.sock".encode()), fallback_cfg([bad, good]))
    assert_empty(good)
    check_unreachable(with_name(p, b"none.sock"), fallback_cfg([NO_PLACE, file]))


# The symlink goes under the first place where the spelling through it fits:
# a place too long even for the widened form is passed over untouched and the
# next one is used; of two short places the first one is used and the second
# is never touched.
def test_the_symlink_goes_under_the_first_place_where_it_fits(tmp: TmpDirs) -> None:
    p = far(tmp)
    long_base = long_dir(tmp, 250)
    first = tmp.new()
    second = tmp.new()
    for d in (long_base, second):
        mark_untouched(d)
    connects_through(p, fallback_cfg([long_base, first]))
    connects_through(with_name(p, b"q.sock"), fallback_cfg([first, second]))
    assert_untouched(long_base)
    assert_untouched(second)
    assert_empty(first)


# The same through the real environment (Config.from_env, as the public API
# reads it): with TMPDIR set to a directory too long for the symlink, a
# 600-byte path is still connected to through a symlink, under /tmp, and
# nothing is created in TMPDIR. Runs in a child process because it sets
# TMPDIR (the cwd is / so the relative spelling is long too).
def test_a_tmpdir_too_long_for_the_symlink_falls_back_to_tmp(tmp: TmpDirs) -> None:
    p = far(tmp)
    long_tmpdir = long_dir(tmp, 250)
    mark_untouched(long_tmpdir)
    with listener_at(p):
        out = run_child("connect_report", b"/", {"PATH": p}, env={"TMPDIR": long_tmpdir})
    assert out["route"] == Route.SYMLINK.value, out
    assert out["bases"] == f'["{os.fsdecode(long_tmpdir)}", "/tmp"]', out
    assert_untouched(long_tmpdir)


# The link's target is the caller's own spelling made absolute against the
# cwd, so a long path given relative to the cwd is reached in the directory it
# names below the cwd (the listener bound there by the routes through D, from the
# relative path too). Runs in a child process with the cwd at a temporary
# directory: the relative spelling is longer than the plain sockaddr, so no
# relative fallback; the symlink under a short place carries it.
@needs_long_bind
def test_a_long_relative_path_is_reached_through_a_link_to_its_absolute_target(tmp: TmpDirs) -> None:
    t = tmp.new()
    base = tmp.new()
    p = nest(t, len(t) + 1 + 300)
    rel = p[len(t) + 1 :]
    out = run_child("relative_input", t, {"REL": rel, "BASE": base})
    assert out["connect"] == Route.SYMLINK.value, out
    assert_empty(base)


# Past the last place nothing is left: ENAMETOOLONG, what a connect answers
# where only the fork route would reach (the SPEC), and no connection reaches
# the listener at the path.
def test_the_symlink_fallback_raises_enametoolong_past_every_place(tmp: TmpDirs) -> None:
    p = far(tmp)
    with listener_at(p) as listener:
        listener.setblocking(False)
        for req in (CONNECT, CONNECT_NONBLOCKING):
            with raises_errno(errno.ENAMETOOLONG):
                _route.run_long(req, p, fallback_cfg())
        with pytest.raises(BlockingIOError):
            listener.accept()
