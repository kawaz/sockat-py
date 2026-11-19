"""``bind_any`` (``sockat.any``): the default bind's routes, and past them,
where the default bind is ``ENAMETOOLONG``, the symlink route: a symlink to
the socket's directory in a place for it, the places only we can write to
before the shared ones (hub SPEC "symlink", DR-0013). What each route loses
(``losses``), the route taken and the routes skipped, on success and on
the errors.

The configuration the public API reads from the environment
(``Config.from_env``) is replaced where a test needs the default bind's
routes all unusable: no place for D, no thread cwd and /proc made to look
absent (:func:`_support.fallback_cfg`), and the places for the symlink
given. That cannot stand in on FreeBSD (``bindat`` needs nothing that can be
turned off, and carries every file name the symlink route would), so the
symlink route is forced elsewhere only."""

from __future__ import annotations

import asyncio
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
    check_route,
    d_cfg,
    d_route_here,
    fallback_cfg,
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
    stale_socket,
    thread_cwd_usable,
    with_name,
)

import sockat
from sockat import _route, _sys
from sockat._route import BIND, CONNECT, OWN_DIR_PREFIX, RANDOM_LEN, Config, Route
from sockat._sys import DIRECT_MAX, IS_FREEBSD, IS_LINUX
from sockat.any import AnyBinding, Loss, Skipped, bind_any

IS_ROOT = os.geteuid() == 0

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


def any_cfg(places: tuple[bytes, ...]) -> Config:
    """No route of the default bind usable (:func:`_support.fallback_cfg`),
    and ``places`` as the places for ``bind_any``'s symlink."""
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
    """The routes :func:`any_cfg` makes ``bind_any`` skip, in order: the
    standard (the path is too long), the routes through D this system has
    (unusable: turned off, /proc absent), then the fork route this
    implementation does not have. No place for D is ever tried, as no means
    is left to use one, so neither is the form without D."""
    names = ["standard"]
    if _sys.HAS_THREAD_CWD:
        names.append("thread-cwd")
    if IS_LINUX:
        names.append("proc")
    return [*names, "fork"]


def names(skipped: tuple[Skipped, ...]) -> list[str]:
    """The names of ``skipped``, after checking every reason says something."""
    assert all(isinstance(s, Skipped) and s.reason for s in skipped), skipped
    return [s.name for s in skipped]


def premise_default_unreachable(p: bytes, places: tuple[bytes, ...]) -> None:
    """The default bind is ``ENAMETOOLONG`` at ``p`` with :func:`any_cfg`,
    and leaves ``places`` (marked untouched here) untouched."""
    for d in places:
        mark_untouched(d)
    with raises_errno(errno.ENAMETOOLONG):
        _route.run_long(BIND, p, any_cfg(places))[0].close()
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
    """The error ``bind_any(path)`` raises."""
    try:
        bind_any(path).listener.close()
    except OSError as e:
        return e
    raise AssertionError("bind_any did not raise")


def connect_here(p: bytes) -> socket.socket:
    """A connect to ``p`` by the routes through D, or the symlink through
    /tmp where they have no means, whatever the public API is made to read
    (:func:`env_cfg`)."""
    return _route.run_long(CONNECT, p, d_cfg(links=(b"/tmp",)))[0]


# ----- the module and the names -----


# The any operations are a choice: `import sockat` neither imports
# sockat.any nor shows bind_any, and importing sockat.any is what makes it
# available. A fresh interpreter, so nothing imported by the tests counts.
def test_import_sockat_does_not_import_sockat_any() -> None:
    code = (
        "import sys, sockat\n"
        "assert 'sockat.any' not in sys.modules, 'sockat.any was imported'\n"
        "assert not hasattr(sockat, 'bind_any') and not hasattr(sockat, 'any')\n"
        "from sockat.any import bind_any\n"
        "assert 'sockat.any' in sys.modules\n"
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
        "fork",
        "thread-cwd-sockdir",
        "at-sockdir",
        "proc-sockdir",
        "fork-sockdir",
        "proc-direct",
        "proc-held",
        "relative",
        "widened",
        "symlink",
        "at-direct",
    }
    assert {r.value for r in Route} == spec_routes


# What bind_any returns: listener, losses, route, skipped, by name or by
# position.
def test_what_bind_any_returns(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 60)
    bound = bind_any(p)
    listener, losses, route, skipped = bound
    with listener:
        assert isinstance(bound, AnyBinding)
        assert (bound.listener, bound.losses, bound.route, bound.skipped) == (listener, losses, route, skipped)


# ----- the default bind's routes -----


# An absolute path the socket module takes goes to it as is: the standard
# route, nothing lost, no route skipped, the standard's socket.
def test_a_short_absolute_path_loses_nothing(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), DIRECT_MAX)
    listener, losses, route, skipped = bind_any(p)
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
    listener, losses, route, _ = bind_any(short)
    with listener:
        assert (losses, route) == ((Loss.CWD_RELATIVE,), "standard")
        assert listener.getsockname() == short
    if d_route_here() is None:
        return
    long = os.path.relpath(os.fsdecode(nest(tmp.new(), 400)))
    assert not long.startswith("/"), long
    assert len(os.fsencode(long)) > DIRECT_MAX, long
    listener, losses, route, _ = bind_any(long)
    with listener:
        assert losses == RESPELLED
        assert route != "standard"


# An abstract address (Linux: a leading NUL) names no file and goes to the
# socket module as is: nothing lost, cwd-relative included.
@pytest.mark.skipif(not IS_LINUX, reason="the abstract namespace is Linux's")
def test_an_abstract_address_loses_nothing() -> None:
    name = "\0sockat-test-" + secrets.token_hex(8)
    listener, losses, route, skipped = bind_any(name)
    with listener:
        assert (losses, route, skipped) == ((), "standard", ())


# Where the default bind reaches a long path, bind_any takes the same route
# (through the public API, from the environment) and loses respelled, having
# skipped only the standard (the path is too long) and, where the system
# refuses a thread its own cwd (Linux under docker's default seccomp),
# thread-cwd.
@needs_long_bind
def test_a_long_path_takes_the_default_bind_route(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 300)
    here = d_route_here()
    assert here is not None
    listener, losses, route, skipped = bind_any(p)
    with listener:
        assert (losses, route) == (RESPELLED, here.value)
        want = ["standard"]
        if IS_LINUX and not thread_cwd_usable():
            want.append("thread-cwd")
        assert names(skipped) == want
        assert listener.getsockname() == os.fsdecode(p)
        client = sockat.connect(p)
        with client:
            accept_ping(listener, client)


# Where D can be made nowhere, bind_any takes the form without D as the
# default bind does, and loses respelled. Skipped first: the standard, every
# route through D of this system (a usable one for want of a place for D),
# the fork route; then the routes of the form without D before the one that
# carries it.
@needs_long_bind
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
    listener, losses, route, skipped = bind_any(p)
    with listener:
        assert (losses, route) == (RESPELLED, sockdir[here].value)
        before = candidates[: candidates.index(here)]
        want = ["standard", *(r.value for r in candidates), "fork", *(sockdir[r].value for r in before)]
        assert names(skipped) == want
        reasons = {s.name: s.reason for s in skipped[1 : 1 + len(candidates)]}
        assert {r for r in candidates if reasons[r.value] == _route.NO_PLACE_FOR_D} == usable, skipped
        accept_ping(listener, connect_here(p))
    assert listing(parent(p)) == [b"s.sock"]


# ----- the symlink route -----


# Past the default bind, a place only we can write to (ours, 0700) carries
# the bind through a symlink, losing respelled only: every check of a route
# (the socket at the path, data both ways, the path reported while the
# kernel keeps the spelling through the symlink, asyncio, nothing left in
# the place), and the routes skipped before it.
@forces_the_symlink_route
def test_past_the_default_bind_a_place_of_our_own_carries_it(tmp: TmpDirs, env_cfg: list[Config]) -> None:
    p = far(tmp)
    own = tmp.new()
    premise_default_unreachable(p, (own,))
    env_cfg.append(any_cfg((own,)))
    listener, losses, route, skipped = bind_any(p)
    with listener:
        assert (losses, route) == (RESPELLED, "symlink")
        assert names(skipped) == skipped_before_the_symlink()
        assert_through(listener, own, p)
    assert_empty(own)
    os.unlink(p)
    connect_route = d_route_here()
    if connect_route is None:
        return

    def way(req: _route.Request) -> tuple[socket.socket, Route]:
        if req.op is _sys.Op.BIND:
            return bind_any(p).listener, Route.SYMLINK
        return _route.run_long(req, p, d_cfg())

    check_route(Forced("symlink", p, way, routes(Route.SYMLINK, connect_route)), [own])


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
    env_cfg.append(any_cfg((shared, own)))
    listener, losses, route, _ = bind_any(p)
    with listener:
        assert (losses, route) == (RESPELLED, "symlink")
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
    env_cfg.append(any_cfg((shared, later)))
    listener, losses, route, _ = bind_any(p)
    with listener:
        assert (losses, route) == (SHARED, "symlink")
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
    env_cfg.append(any_cfg((*unsafe, t + b"/missing", file, shared)))
    listener, losses, route, _ = bind_any(p)
    with listener:
        assert (losses, route) == (SHARED, "symlink")
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
    env_cfg.append(any_cfg((too_long, just)))
    listener, _, route, _ = bind_any(p)
    with listener:
        assert route == "symlink"
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
    env_cfg.append(any_cfg((locked, own)))
    listener, _, _, _ = bind_any(p)
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
# the standard for a short path (nothing skipped); the default bind's route
# for a long one.
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
    assert getattr(e, "route") == here.value  # noqa: B009
    want = ["standard"]
    if IS_LINUX and not thread_cwd_usable():
        want.append("thread-cwd")
    assert names(getattr(e, "skipped")) == want  # noqa: B009


# A missing directory is ENOENT (a FileNotFoundError, as from sockat.bind)
# before any route's bind is made: no route, the standard skipped.
def test_a_missing_directory_tells_no_route(tmp: TmpDirs) -> None:
    p = with_name(nest(tmp.new(), 300), b"missing/s.sock")
    e = raised(p)
    assert isinstance(e, FileNotFoundError)
    assert e.errno == errno.ENOENT
    assert getattr(e, "route") is None  # noqa: B009
    assert names(getattr(e, "skipped")) == ["standard"]  # noqa: B009


# A file name no route carries (too long for R/<name>, for the form without
# D and for the symlink through /tmp) is ENAMETOOLONG with every route
# skipped, through the public API from the environment: the standard, the
# routes through D this system has (the spelling too long), the fork route;
# no route got as far as a bind.
def test_a_file_name_no_route_carries_tells_every_route_skipped(tmp: TmpDirs) -> None:
    p = with_name(far(tmp), b"n" * 100)
    e = raised(p)
    assert e.errno == errno.ENAMETOOLONG
    assert getattr(e, "route") is None  # noqa: B009
    want = ["standard"]
    if IS_FREEBSD:
        want.append("at")
    elif _sys.HAS_THREAD_CWD:
        want.append("thread-cwd")
    if IS_LINUX:
        want.append("proc")
    assert names(getattr(e, "skipped")) == [*want, "fork"]  # noqa: B009
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
    env_cfg.append(any_cfg((first, second)))
    e = raised(p)
    assert e.errno == errno.EADDRINUSE
    assert getattr(e, "route") == "symlink"  # noqa: B009
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
    env_cfg.append(any_cfg(()))
    e = raised(p)
    assert e.errno == errno.ENAMETOOLONG
    assert getattr(e, "route") is None  # noqa: B009
    assert names(getattr(e, "skipped")) == skipped_before_the_symlink()  # noqa: B009
    assert listing(parent(p)) == []


# An argument error (a NUL in the path) is the ValueError sockat.bind
# raises, with nothing skipped and no route.
def test_an_argument_error_is_the_value_error() -> None:
    with pytest.raises(ValueError, match="NUL") as e:
        bind_any("/tmp/a\0b")
    assert (getattr(e.value, "route"), getattr(e.value, "skipped")) == (None, ())  # noqa: B009


# ----- asyncio -----


# The listener of bind_any is handed to asyncio as the standard's is: at a
# long path from the environment, and through the symlink route.
@needs_long_bind
def test_the_listener_is_served_by_asyncio(tmp: TmpDirs) -> None:
    p = nest(tmp.new(), 400)

    def connect(req: _route.Request) -> socket.socket:
        return sockat.connect_nonblocking(p) if req.nonblocking else sockat.connect(p)

    asyncio.run(serve_asyncio("bind_any", bind_any(p).listener, connect, os.fsdecode(p), served=True))
    assert_cleaned_up_as_asyncio_does("bind_any", p)
    if IS_FREEBSD:
        return
    own = tmp.new()
    listener = _route.run_long(_route.BIND_ANY, p, any_cfg((own,)))[0]
    asyncio.run(serve_asyncio("symlink", listener, connect, os.fsdecode(p), served=True))
    assert_cleaned_up_as_asyncio_does("symlink", p)
    assert_empty(own)
