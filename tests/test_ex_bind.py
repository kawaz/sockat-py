"""``sockat.ex.bind`` (hub SPEC "_ex", DR-0015): the default bind's routes,
and with ``last_resort`` past them, where the default bind is
``ENAMETOOLONG``, symlink-dir: a symlink to the socket's directory in a
place for it, the places only we can write to before the shared ones (hub
SPEC "symlink-dir"). What each route loses (``losses``), the route taken and
the routes skipped, on success and on the errors. ``discard`` is checked in
``test_ex.py``, the connects of ``sockat.ex`` in ``test_ex_connect.py``.

The configuration the public API reads from the environment
(``Config.from_env``) is replaced where a test needs the default bind's
routes all unusable: no place for D, no thread cwd and /proc made to look
absent (:func:`_support.fallback_cfg`), and the places for the symlink
given. That cannot stand in on FreeBSD (``bindat`` needs nothing that can be
turned off, and carries every file name the symlink route would), so the
symlink route is forced elsewhere only."""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import os
import re
import secrets
import socket
import subprocess
import sys

import pytest
from _support import (
    HAS_PROC,
    SRC_DIR,
    Forced,
    TmpDirs,
    accept_ping,
    assert_cleaned_up_as_asyncio_does,
    assert_empty,
    assert_far_from_cwd,
    assert_untouched,
    bind_route_here,
    check_route,
    d_cfg,
    d_route_here,
    fallback_cfg,
    far_connect_here,
    is_socket,
    kernel_sockname,
    listing,
    mark_untouched,
    needs_long_bind,
    nest,
    parent,
    place,
    raises_errno,
    routes,
    serve_asyncio,
    skipped_before_d_here,
    stale_socket,
    thread_cwd_usable,
    with_name,
)

import sockat
import sockat.ex
from sockat import _route, _sys
from sockat._route import BIND, CONNECT, OWN_DIR_PREFIX, RANDOM_LEN, Config, Request, Route
from sockat._sys import DIRECT_MAX, IS_FREEBSD, IS_LINUX
from sockat.ex import Bound, Loss, Skipped

IS_ROOT = os.geteuid() == 0

BIND_LAST_RESORT = Request(_sys.Op.BIND, last_resort=True)
"""A bind with the last resort, for the route order called with a
configuration of the test's own."""

NOBODY = 65534
"""Another user, for the root runs (docker's ``nobody``)."""

forces_the_symlink_route = pytest.mark.skipif(
    IS_FREEBSD, reason="bindat cannot be made unusable, so no bind past the default routes can be stood in for"
)

LINK = rb"/sockat\.[A-Za-z0-9]{%d}/" % RANDOM_LEN
"""The symlink's part of the kernel's spelling through the symlink route."""

RESPELLED = (Loss.RESPELLED,)
SHARED = (Loss.RESPELLED, Loss.RECREATABLE_SPELLING)


def far(tmp: TmpDirs) -> bytes:
    p = nest(tmp.new(), 600)
    assert_far_from_cwd(p)
    return p


def last_resort_cfg(places: tuple[bytes, ...]) -> Config:
    """No route of the default bind usable (:func:`_support.fallback_cfg`),
    and ``places`` as the places for symlink-dir's symlink."""
    c = fallback_cfg()
    return Config(
        own_dir_places=c.own_dir_places,
        link_places=c.link_places,
        bind_link_places=places,
        wide=c.wide,
        proc_self_fd=c.proc_self_fd,
        thread_cwd=c.thread_cwd,
    )


@pytest.fixture
def env_cfg(monkeypatch: pytest.MonkeyPatch) -> list[Config]:
    """Have the public API read the configuration put in the list (its last
    item) instead of the environment (the connects too: connect through
    :func:`connect_here`)."""
    cfgs: list[Config] = []
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: cfgs[-1]))
    return cfgs


def skipped_before_the_symlink() -> list[str]:
    """The routes :func:`last_resort_cfg` makes ``sockat.ex.bind`` skip, in order: the
    standard (the path is too long), symlink-file where a bind tries it
    (every system but Linux; no place for its symlink), the routes through D
    this system has (unusable: turned off, /proc absent), then the fork route
    this implementation does not have. No place for D is ever tried, as no
    means is left to use one, so neither is the form without D."""
    names = ["standard"]
    if not IS_LINUX:
        names.append("symlink-file")
    if _sys.HAS_THREAD_CWD:
        names.append("thread-cwd")
    if IS_LINUX:
        names.append("proc")
    return [*names, "fork-cwd"]


def names(skipped: tuple[Skipped, ...]) -> list[str]:
    """The names of ``skipped``, after checking every reason says something."""
    assert all(isinstance(s, Skipped) and s.reason for s in skipped), skipped
    return [s.name for s in skipped]


def premise_default_unreachable(p: bytes, places: tuple[bytes, ...]) -> None:
    """The default bind is ``ENAMETOOLONG`` at ``p`` with :func:`last_resort_cfg`,
    and leaves ``places`` (marked untouched here) untouched."""
    for d in places:
        mark_untouched(d)
    with raises_errno(errno.ENAMETOOLONG):
        _route.run_long(BIND, p, last_resort_cfg(places))[0].close()
    for d in places:
        assert_untouched(d)


def assert_through(listener: socket.socket, place_dir: bytes, p: bytes) -> None:
    """``listener`` was bound through a symlink in ``place_dir``: the kernel
    keeps ``<place_dir>/sockat.XXXXXXXXXX/<name>``, while the listener
    reports the path ``p``, where the socket file is."""
    spelled = os.fsencode(kernel_sockname(listener))
    assert re.fullmatch(re.escape(place_dir) + LINK + re.escape(os.path.basename(p)), spelled), spelled
    assert listener.getsockname() == os.fsdecode(p)
    assert is_socket(p)


def raised(path: bytes) -> OSError:
    """The error ``sockat.ex.bind(path, last_resort=True)`` raises."""
    try:
        sockat.ex.bind(path, last_resort=True).listener.close()
    except OSError as e:
        return e
    raise AssertionError("sockat.ex.bind did not raise")


def connect_here(p: bytes) -> socket.socket:
    """A connect to ``p`` by the connect order of this system with the real
    /proc and thread cwd and /tmp for the symlink, whatever the public API is
    made to read (:func:`env_cfg`)."""
    return _route.run_long(CONNECT, p, d_cfg(links=(b"/tmp",)))[0]


# ----- the module and the names -----


# The _ex calls are a choice: `import sockat` neither imports sockat.ex nor
# shows it (sockat shows its three plain calls only), and importing
# sockat.ex is what makes it available, with its bind, connect and
# connect_nonblocking (DR-0015). A fresh interpreter, so nothing imported by
# the tests counts.
def test_import_sockat_does_not_import_sockat_ex() -> None:
    code = (
        "import sys, sockat\n"
        "assert 'sockat.ex' not in sys.modules, 'sockat.ex was imported'\n"
        "assert not hasattr(sockat, 'ex')\n"
        "assert sockat.__all__ == ['bind', 'connect', 'connect_nonblocking'], sockat.__all__\n"
        "import sockat.ex\n"
        "assert 'sockat.ex' in sys.modules\n"
        "assert {'bind', 'connect', 'connect_nonblocking'} <= set(sockat.ex.__all__)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr


# The names are the SPEC's: a loss is its SPEC name as a string (str(),
# .value, formatting), and so is a route.
def test_the_names_are_the_specs() -> None:
    spec_losses = {"respelled", "forked", "recreatable-spelling", "cwd-relative"}
    assert {str(loss) for loss in Loss} == {loss.value for loss in Loss} == {f"{loss}" for loss in Loss} == spec_losses
    assert Loss("cwd-relative") is Loss.CWD_RELATIVE
    spec_routes = {
        "standard",
        "thread-cwd",
        "at",
        "proc",
        "fork-cwd",
        "thread-cwd-sockdir",
        "at-sockdir",
        "proc-sockdir",
        "fork-cwd-sockdir",
        "proc-direct",
        "proc-held",
        "relative",
        "widened",
        "symlink-file",
        "symlink-dir",
        "at-direct",
        "fork-cwd-direct",
    }
    assert {r.value for r in Route} == spec_routes


# What sockat.ex.bind returns: listener, discard, losses, route, skipped, by
# name or by position; the same with the last resort or without.
@pytest.mark.parametrize("last_resort", [False, True])
def test_what_bind_returns(tmp: TmpDirs, last_resort: bool) -> None:
    p = nest(tmp.new(), 60)
    bound = sockat.ex.bind(p, last_resort=last_resort)
    listener, discard, losses, route, skipped = bound
    with listener:
        assert isinstance(bound, Bound)
        assert (bound.listener, bound.discard, bound.losses, bound.route, bound.skipped) == (
            listener,
            discard,
            losses,
            route,
            skipped,
        )
        assert (losses, route, skipped) == ((), "standard", ())
    discard()
    assert not os.path.lexists(p)


# ----- the default bind's routes -----


# An absolute path the socket module takes goes to it as is: the standard
# route, nothing lost, no route skipped, the standard's socket.
def test_a_short_absolute_path_loses_nothing(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), DIRECT_MAX)
    listener, _, losses, route, skipped = sockat.ex.bind(p, last_resort=True)
    with listener:
        assert (losses, route, skipped) == ((), "standard", ())
        assert type(listener) is socket.socket
        assert listener.getsockname() == os.fsdecode(p)
        client = sockat.connect(p)
        with client:
            accept_ping(listener, client)


# A relative path the socket module takes goes to it as is too, and the
# kernel keeps that spelling, relative to the cwd: cwd-relative. A long
# relative path goes through D and loses respelled only (the kernel keeps
# R/<name>, not the path given).
def test_a_relative_path_loses_cwd_relative_on_the_standard_only(tmp: TmpDirs) -> None:
    short = os.path.relpath(os.fsdecode(nest(tmp.new(), 60)))
    assert not short.startswith("/"), short
    assert len(os.fsencode(short)) <= DIRECT_MAX, short
    listener, _, losses, route, _ = sockat.ex.bind(short, last_resort=True)
    with listener:
        assert (losses, route) == ((Loss.CWD_RELATIVE,), "standard")
        assert listener.getsockname() == short
    if d_route_here() is None:
        return
    long = os.path.relpath(os.fsdecode(nest(tmp.new(), 400)))
    assert not long.startswith("/"), long
    assert len(os.fsencode(long)) > DIRECT_MAX, long
    listener, _, losses, route, _ = sockat.ex.bind(long, last_resort=True)
    with listener:
        assert losses == RESPELLED
        assert route != "standard"


# An abstract address (Linux: a leading NUL) names no file and goes to the
# socket module as is: nothing lost, cwd-relative included.
@pytest.mark.skipif(not IS_LINUX, reason="the abstract namespace is Linux's")
def test_an_abstract_address_loses_nothing() -> None:
    name = "\0sockat-test-" + secrets.token_hex(8)
    listener, _, losses, route, skipped = sockat.ex.bind(name, last_resort=True)
    with listener:
        assert (losses, route, skipped) == ((), "standard", ())


# Where the default bind reaches a long path, sockat.ex.bind takes the same route
# (through the public API, from the environment) and loses respelled: the
# symlink-file route where the bind follows the link (macOS; SPEC: tried
# right after the standard on every system but Linux), else the routes
# through D. Skipped: the standard (the path is too long), symlink-file where
# the bind does not follow the link, and, where the system refuses a thread
# its own cwd (Linux under docker's default seccomp), thread-cwd.
@needs_long_bind
def test_a_long_path_takes_the_default_bind_route(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 300)
    here = bind_route_here()
    assert here is not None
    listener, _, losses, route, skipped = sockat.ex.bind(p, last_resort=True)
    with listener:
        assert (losses, route) == (RESPELLED, here.value)
        want = skipped_before_d_here() if here is not Route.SYMLINK_FILE else ["standard"]
        if IS_LINUX and not thread_cwd_usable():
            want.append("thread-cwd")
        assert names(skipped) == want
        assert listener.getsockname() == os.fsdecode(p)
        client = sockat.connect(p)
        with client:
            accept_ping(listener, client)


# Where D can be made nowhere, sockat.ex.bind takes the form without D as the
# default bind does, and loses respelled. Skipped first: the standard,
# symlink-file where a bind tries it (no place for its symlink here), every
# route through D of this system (a usable one for want of a place for D),
# the fork route; then the routes of the form without D before the one that
# carries it.
@pytest.mark.skipif(d_route_here() is None, reason="no means of the routes through D here")
def test_where_d_can_be_made_nowhere_the_form_without_d_is_taken(tmp: TmpDirs, env_cfg: list[Config]) -> None:
    here = d_route_here()
    assert here is not None
    candidates = [Route.AT] if IS_FREEBSD else [Route.THREAD_CWD, *([Route.PROC] if IS_LINUX else [])]
    usable = {Route.AT} if IS_FREEBSD else {Route.THREAD_CWD} if thread_cwd_usable() else set()
    if HAS_PROC:
        usable.add(Route.PROC)
    sockdir = {Route.AT: Route.AT_SOCKDIR, Route.THREAD_CWD: Route.THREAD_CWD_SOCKDIR, Route.PROC: Route.PROC_SOCKDIR}
    p = far(tmp)
    env_cfg.append(d_cfg(own=()))
    listener, _, losses, route, skipped = sockat.ex.bind(p, last_resort=True)
    with listener:
        assert (losses, route) == (RESPELLED, sockdir[here].value)
        before = candidates[: candidates.index(here)]
        first = ["standard", *([] if IS_LINUX else ["symlink-file"])]
        want = [*first, *(r.value for r in candidates), "fork-cwd", *(sockdir[r].value for r in before)]
        assert names(skipped) == want
        reasons = {s.name: s.reason for s in skipped[len(first) : len(first) + len(candidates)]}
        assert {r for r in candidates if reasons[r.value] == _route.NO_PLACE_FOR_D} == usable, skipped
        accept_ping(listener, connect_here(p))
    assert listing(parent(p)) == [b"s.sock"]


# ----- the symlink route -----


# Past the default bind, a place only we can write to (ours, 0700) carries
# the bind through a symlink, losing respelled only: every check of a route
# (the socket at the path, data both ways, the path reported while the
# kernel keeps the spelling through the symlink, asyncio, nothing left in
# the place), and the routes skipped before it. The connects take the
# connect order of the SPEC with the real configuration, whose first route
# that can be used here (far_connect_here) carries them.
@forces_the_symlink_route
def test_past_the_default_bind_a_place_of_our_own_carries_it(tmp: TmpDirs, env_cfg: list[Config]) -> None:
    p = far(tmp)
    own = tmp.new()
    premise_default_unreachable(p, (own,))
    env_cfg.append(last_resort_cfg((own,)))
    listener, _, losses, route, skipped = sockat.ex.bind(p, last_resort=True)
    with listener:
        assert (losses, route) == (RESPELLED, "symlink-dir")
        assert names(skipped) == skipped_before_the_symlink()
        assert_through(listener, own, p)
    assert_empty(own)
    os.unlink(p)
    connect_route = far_connect_here()
    if connect_route is None:
        return

    def way(req: _route.Request) -> tuple[socket.socket, Route]:
        if req.op is _sys.Op.BIND:
            return sockat.ex.bind(p, last_resort=True).listener, Route.SYMLINK_DIR
        return _route.run_long(req, p, d_cfg())

    check_route(Forced("symlink", p, way, routes(Route.SYMLINK_DIR, connect_route)), [own])


# A place only we can write to is used before a shared one (sticky, ours or
# root's), whatever their order among the places: the shared one is not
# touched.
@forces_the_symlink_route
def test_a_place_of_our_own_is_tried_before_a_shared_one(tmp: TmpDirs, env_cfg: list[Config]) -> None:
    p = far(tmp)
    shared = place(tmp.new(), b"shared", 0o1777)
    own = tmp.new()
    assert _route.place_kind(shared) is _route.PlaceKind.SHARED
    assert _route.place_kind(own) is _route.PlaceKind.OWN
    premise_default_unreachable(p, (shared, own))
    env_cfg.append(last_resort_cfg((shared, own)))
    listener, _, losses, route, _ = sockat.ex.bind(p, last_resort=True)
    with listener:
        assert (losses, route) == (RESPELLED, "symlink-dir")
        assert_through(listener, own, p)
    assert_untouched(shared)
    assert_empty(own)


# With only shared places, the first that carries the bind is used, losing
# respelled and recreatable-spelling; nothing is left there.
@forces_the_symlink_route
def test_a_shared_place_alone_carries_it_with_a_recreatable_spelling(tmp: TmpDirs, env_cfg: list[Config]) -> None:
    p = far(tmp)
    shared = place(tmp.new(), b"shared", 0o1777)
    later = place(tmp.new(), b"later", 0o1777)
    premise_default_unreachable(p, (shared, later))
    env_cfg.append(last_resort_cfg((shared, later)))
    listener, _, losses, route, _ = sockat.ex.bind(p, last_resort=True)
    with listener:
        assert (losses, route) == (SHARED, "symlink-dir")
        assert_through(listener, shared, p)
        client = connect_here(p)
        with client:
            accept_ping(listener, client)
    assert os.stat(shared).st_mtime != 0
    assert_empty(shared)
    assert_untouched(later)


# A place that is neither ours alone nor a shared one (writable by others
# and not sticky, writable by the group, missing, not a directory), or
# another user's sticky one (root only: other users cannot give one away),
# is passed over with nothing made in it, for the next place of either kind.
@forces_the_symlink_route
def test_a_place_that_is_not_safe_is_passed_over(tmp: TmpDirs, env_cfg: list[Config]) -> None:
    p = far(tmp)
    t = tmp.new()
    unsafe = [place(t, b"o+w", 0o777), place(t, b"g+w", 0o770)]
    if IS_ROOT:
        theirs = place(t, b"theirs", 0o1777)
        os.chown(theirs, NOBODY, NOBODY)
        unsafe.append(theirs)
    file = t + b"/file"
    with open(file, "wb"):
        pass
    for d in unsafe:
        assert _route.place_kind(d) is None, d
        mark_untouched(d)
    shared = place(tmp.new(), b"shared", 0o1777)
    env_cfg.append(last_resort_cfg((*unsafe, t + b"/missing", file, shared)))
    listener, _, losses, route, _ = sockat.ex.bind(p, last_resort=True)
    with listener:
        assert (losses, route) == (SHARED, "symlink-dir")
        assert_through(listener, shared, p)
    for d in unsafe:
        assert_untouched(d)


# A place where the spelling through it (`<place>/sockat.XXXXXXXXXX/<name>`)
# does not fit sun_path is passed over before anything is made in it; one
# whose spelling just fits is used.
@forces_the_symlink_route
def test_a_place_too_long_for_the_spelling_is_passed_over(tmp: TmpDirs, env_cfg: list[Config]) -> None:
    p = far(tmp)
    name = os.path.basename(p)
    fits = DIRECT_MAX - len(b"/" + OWN_DIR_PREFIX + b"X" * RANDOM_LEN + b"/" + name)
    too_long = nest(tmp.new(), fits + 1, b"place")
    just = nest(tmp.new(), fits, b"place")
    for d in (too_long, just):
        os.mkdir(d)
        mark_untouched(d)
    env_cfg.append(last_resort_cfg((too_long, just)))
    listener, _, _, route, _ = sockat.ex.bind(p, last_resort=True)
    with listener:
        assert route == "symlink-dir"
        assert_through(listener, just, p)
        assert len(os.fsencode(kernel_sockname(listener))) == DIRECT_MAX
    assert_untouched(too_long)
    assert_empty(just)


# A place where the symlink cannot be created (ours, but without write
# permission) is passed over for the next one. Root writes anywhere.
@forces_the_symlink_route
@pytest.mark.skipif(IS_ROOT, reason="root bypasses the permission under test")
def test_a_place_where_the_symlink_cannot_be_created_is_passed_over(tmp: TmpDirs, env_cfg: list[Config]) -> None:
    p = far(tmp)
    locked = place(tmp.new(), b"locked", 0o500)
    own = tmp.new()
    assert _route.place_kind(locked) is _route.PlaceKind.OWN
    premise_default_unreachable(p, (locked, own))
    env_cfg.append(last_resort_cfg((locked, own)))
    listener, _, _, _, _ = sockat.ex.bind(p, last_resort=True)
    with listener:
        assert_through(listener, own, p)
    assert_untouched(locked)
    assert_empty(own)


# The places read from the environment: XDG_RUNTIME_DIR, then TMPDIR, when
# each is an absolute path, then /tmp; a place spelled as /tmp or as an
# earlier one is left out.
def test_the_places_for_the_symlink_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    def places(tmpdir: bytes | None, xdg: bytes | None) -> tuple[bytes, ...]:
        return Config.from_tmpdir(tmpdir, xdg).bind_link_places

    assert places(None, None) == places(b"", b"") == places(b"rel", b"rel") == (b"/tmp",)
    assert places(b"/t", b"/run/user/1") == (b"/run/user/1", b"/t", b"/tmp")
    assert places(b"/t", None) == (b"/t", b"/tmp")
    assert places(None, b"/x") == (b"/x", b"/tmp")
    assert places(b"/same/", b"//same") == (b"//same", b"/tmp")
    assert places(b"/tmp/", b"/tmp") == (b"/tmp",)
    assert Config.from_tmpdir(b"/t", b"/x").link_places == (b"/t", b"/tmp")
    monkeypatch.setenv("TMPDIR", "/t")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/x")
    assert Config.from_env().bind_link_places == (b"/x", b"/t", b"/tmp")


# ----- errors -----


# A bind that fails is the error sockat.bind raises (its type and errno),
# with the routes skipped up to the failure and the route whose bind failed:
# the standard for a short path (nothing skipped); the default bind's first
# route that binds for a long one: symlink-file where a bind tries it (every
# system but Linux; with a socket file at the path its failure is the
# outcome whether or not the kernel followed the link), else the routes
# through D.
def test_a_failed_bind_tells_the_route_and_what_was_skipped(tmp: TmpDirs) -> None:
    short = nest(tmp.new(), 60)
    stale_socket(short)
    e = raised(short)
    assert e.errno == errno.EADDRINUSE
    assert (getattr(e, "route"), getattr(e, "skipped")) == ("standard", ())  # noqa: B009
    here = d_route_here()
    if here is None:
        return
    long = nest(tmp.new(), 300)
    stale_socket(long)
    e = raised(long)
    assert e.errno == errno.EADDRINUSE
    failed = here if IS_LINUX else Route.SYMLINK_FILE
    assert getattr(e, "route") == failed.value  # noqa: B009
    want = ["standard"]
    if IS_LINUX and not thread_cwd_usable():
        want.append("thread-cwd")
    assert names(getattr(e, "skipped")) == want  # noqa: B009


# A missing directory is ENOENT (a FileNotFoundError, as from sockat.bind)
# before any route is tried: routes are noted only once the directory has
# been looked up (SPEC), so no route and nothing skipped, not even the
# standard.
def test_a_missing_directory_tells_no_route(tmp: TmpDirs) -> None:
    p = with_name(nest(tmp.new(), 300), b"missing/s.sock")
    e = raised(p)
    assert isinstance(e, FileNotFoundError)
    assert e.errno == errno.ENOENT
    assert getattr(e, "route") is None  # noqa: B009
    assert names(getattr(e, "skipped")) == []  # noqa: B009


# A file name no route carries (too long for R/<name>, for the form without
# D and for symlink-dir's symlink through /tmp) is ENAMETOOLONG with every route
# skipped, through the public API from the environment with no place for
# symlink-file (which carries any file name where the bind follows the link,
# as on macOS): the standard, symlink-file where a bind tries it, the routes
# through D this system has (the spelling too long), the fork route; no
# route got as far as a bind.
def test_a_file_name_no_route_carries_tells_every_route_skipped(tmp: TmpDirs, env_cfg: list[Config]) -> None:
    p = with_name(far(tmp), b"n" * 100)
    from_env = Config.from_tmpdir(os.environb.get(b"TMPDIR"), os.environb.get(b"XDG_RUNTIME_DIR"))
    env_cfg.append(dataclasses.replace(from_env, link_places=()))
    e = raised(p)
    assert e.errno == errno.ENAMETOOLONG
    assert getattr(e, "route") is None  # noqa: B009
    want = ["standard"]
    if not IS_LINUX:
        want.append("symlink-file")
    if IS_FREEBSD:
        want.append("at")
    elif _sys.HAS_THREAD_CWD:
        want.append("thread-cwd")
    if IS_LINUX:
        want.append("proc")
    assert names(getattr(e, "skipped")) == [*want, "fork-cwd"]  # noqa: B009
    assert not os.path.lexists(p)


# A bind through the symlink that fails (EADDRINUSE at a path where a socket
# file is left) is the outcome: the route is symlink, nothing is tried in
# the next place, and nothing is left in the first.
@forces_the_symlink_route
def test_a_failure_through_the_symlink_is_not_retried(tmp: TmpDirs, env_cfg: list[Config]) -> None:
    p = far(tmp)
    stale_socket(p)
    first = tmp.new()
    second = tmp.new()
    for d in (first, second):
        mark_untouched(d)
    env_cfg.append(last_resort_cfg((first, second)))
    e = raised(p)
    assert e.errno == errno.EADDRINUSE
    assert getattr(e, "route") == "symlink-dir"  # noqa: B009
    assert names(getattr(e, "skipped")) == skipped_before_the_symlink()  # noqa: B009
    assert os.stat(first).st_mtime != 0
    assert_empty(first)
    assert_untouched(second)
    assert listing(parent(p)) == [b"s.sock"]


# With no place that carries it either, ENAMETOOLONG: no route, every route
# skipped, nothing made.
@forces_the_symlink_route
def test_no_place_for_the_symlink_is_enametoolong(tmp: TmpDirs, env_cfg: list[Config]) -> None:
    p = far(tmp)
    env_cfg.append(last_resort_cfg(()))
    e = raised(p)
    assert e.errno == errno.ENAMETOOLONG
    assert getattr(e, "route") is None  # noqa: B009
    assert names(getattr(e, "skipped")) == skipped_before_the_symlink()  # noqa: B009
    assert listing(parent(p)) == []


# Without the last resort, sockat.ex.bind takes the default bind's routes
# only: where only symlink-dir would carry the path (a place of our own given
# for it), it is ENAMETOOLONG as sockat.bind is, with every route of the
# default bind skipped, symlink-dir not among them (not in the order), and
# nothing made in the place.
@forces_the_symlink_route
def test_without_the_last_resort_it_is_the_default_bind(tmp: TmpDirs, env_cfg: list[Config]) -> None:
    p = far(tmp)
    own = tmp.new()
    mark_untouched(own)
    env_cfg.append(last_resort_cfg((own,)))
    with pytest.raises(OSError, match="too long") as e:
        sockat.ex.bind(p)
    assert e.value.errno == errno.ENAMETOOLONG
    assert getattr(e.value, "route") is None  # noqa: B009
    assert names(getattr(e.value, "skipped")) == skipped_before_the_symlink()  # noqa: B009
    assert_untouched(own)
    assert listing(parent(p)) == []
    listener, _, losses, route, _ = sockat.ex.bind(p, last_resort=True)
    with listener:
        assert (losses, route) == (RESPELLED, "symlink-dir")


# An argument error (a NUL in the path) is the ValueError sockat.bind
# raises, with nothing skipped and no route.
def test_an_argument_error_is_the_value_error() -> None:
    with pytest.raises(ValueError, match="NUL") as e:
        sockat.ex.bind("/tmp/a\0b", last_resort=True)
    assert (getattr(e.value, "route"), getattr(e.value, "skipped")) == (None, ())  # noqa: B009


# ----- asyncio -----


# The listener of sockat.ex.bind is handed to asyncio as the standard's is:
# at a long path from the environment, and through symlink-dir.
@needs_long_bind
def test_the_listener_is_served_by_asyncio(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 400)

    def connect(req: _route.Request) -> socket.socket:
        return sockat.connect_nonblocking(p) if req.nonblocking else sockat.connect(p)

    listener = sockat.ex.bind(p, last_resort=True).listener
    asyncio.run(serve_asyncio("sockat.ex.bind", listener, connect, os.fsdecode(p), served=True))
    assert_cleaned_up_as_asyncio_does("sockat.ex.bind", p)
    if IS_FREEBSD:
        return
    own = tmp.new()
    listener = _route.run_long(BIND_LAST_RESORT, p, last_resort_cfg((own,)))[0]
    asyncio.run(serve_asyncio("symlink-dir", listener, connect, os.fsdecode(p), served=True))
    assert_cleaned_up_as_asyncio_does("symlink-dir", p)
    assert_empty(own)
