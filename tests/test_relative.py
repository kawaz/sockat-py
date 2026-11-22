"""Relative paths of bind and resolve made absolute (hub SPEC "相対パスを絶対パスにする",
SPEC-routes "絶対パスの候補の作り方", DR-0019), the real candidate of a long
absolute path, and connect keeping a relative path as it is. Each call that
reads the cwd runs in a child process of its own (its cwd and ``$PWD`` set
there)."""

from __future__ import annotations

import errno
import os
import socket
import sys

import pytest
from _support import (
    HAS_PROC,
    NO_ROUTE_CODE,
    THREAD_ROUTE_IN_BIND_ROW,
    TmpDirs,
    deep_dir,
    is_socket,
    listener_at,
    long_path,
    raises_errno,
    run_child,
    standard_errno,
)

import sockat.ex
from sockat._sys import DIRECT_MAX

IS_FREEBSD = sys.platform.startswith("freebsd")


def real(p: bytes) -> bytes:
    return os.path.realpath(p)


def long_real_dir(tmp: TmpDirs) -> bytes:
    """A directory whose real path with ``/s.sock`` does not fit the
    standard (108 bytes and more)."""
    d = real(deep_dir(tmp.new(), 110))
    assert len(d + b"/s.sock") > DIRECT_MAX
    return d


BIND_AND_REPORT = """
import sockat, sockat.ex
b = sockat.ex.bind("s.sock")
report(
    spelling=type(b.spelling).__name__,
    route=b.route,
    skipped=[s.name for s in b.skipped],
    name=b.socket.getsockname(),
    kernel=socket.socket.getsockname(b.socket),
)
os.chdir("/")
b.file.remove()
b.socket.close()
"""
"""A child's bind of the relative ``s.sock`` from its cwd, then a change of
cwd and ``remove()``."""


# A short relative path is made absolute and goes to the standard as its real
# candidate (the realpath of the cwd and the file name): Original, no route
# skipped, the kernel keeps the absolute spelling (getsockname). After the cwd
# has changed, remove() removes the file bound.
def test_a_short_relative_bind_goes_to_the_standard_absolute(tmp: TmpDirs) -> None:
    d = tmp.new()
    out = run_child(BIND_AND_REPORT, cwd=d)
    assert out["spelling"] == "Original"
    assert out["route"] == "standard"
    assert out["skipped"] == []
    assert out["kernel"] == out["name"] == os.fsdecode(real(d) + b"/s.sock")
    assert not os.path.lexists(d + b"/s.sock")


# The default bind does the same: the listener's address is the absolute
# spelling, so a close that unlinks by the address, or a reply to it, does not
# depend on the cwd.
def test_the_default_bind_makes_a_relative_path_absolute(tmp: TmpDirs) -> None:
    d = tmp.new()
    out = run_child(
        """
        import sockat
        s = sockat.bind("s.sock")
        report(name=s.getsockname())
        """,
        cwd=d,
    )
    assert out["name"] == os.fsdecode(real(d) + b"/s.sock")


# The real candidate does not fit (the cwd's real path is deep) but the $PWD
# candidate does (the cwd reached through a short symlink, $PWD that
# symlink): the standard gets the $PWD spelling, Original, and the socket is
# made in the cwd.
def test_the_pwd_candidate_when_the_real_one_does_not_fit(tmp: TmpDirs) -> None:
    deep = long_real_dir(tmp)
    link = tmp.new() + b"/l"
    os.symlink(deep, link)
    out = run_child(BIND_AND_REPORT, cwd=link)
    assert out["spelling"] == "Original"
    assert out["route"] == "standard"
    assert out["kernel"] == os.fsdecode(link + b"/s.sock")
    assert not os.path.lexists(deep + b"/s.sock")


# $PWD that is not the cwd (inherited, not set again after a change of
# directory) is not used: with the real candidate too long, the path goes to
# the routes (link-dir), and nothing is made where $PWD points.
def test_a_pwd_that_is_not_the_cwd_is_not_used(tmp: TmpDirs) -> None:
    deep = long_real_dir(tmp)
    elsewhere = tmp.new()
    out = run_child(BIND_AND_REPORT, cwd=deep, env={"PWD": elsewhere})
    assert out["spelling"] == "Linked"
    assert out["route"] == "link-dir"
    assert out["skipped"] == ["standard"]
    assert os.listdir(elsewhere) == []


# A path through a symlink and then "..": folded as text, the $PWD candidate
# would name the cwd, while the kernel takes the parent of what the symlink
# points to. The folded directory is not the one meant, so the candidate is
# not used; the socket is made where the kernel means (through a route, the
# real candidate being too long), and nothing in the cwd.
def test_a_pwd_candidate_folded_to_another_directory_is_not_used(tmp: TmpDirs) -> None:
    cwd_real = long_real_dir(tmp)
    link = tmp.new() + b"/l"
    os.symlink(cwd_real, link)
    meant = long_real_dir(tmp)
    os.mkdir(meant + b"/inner")
    os.symlink(meant + b"/inner", cwd_real + b"/sym")
    out = run_child(
        """
        import sockat.ex
        b = sockat.ex.bind("sym/../s.sock")
        report(spelling=type(b.spelling).__name__, route=b.route)
        """,
        cwd=link,
    )
    assert out["spelling"] == "Linked"
    assert out["route"] == "link-dir"
    assert is_socket(meant + b"/s.sock")
    assert not os.path.lexists(cwd_real + b"/s.sock")


# "../.." folded as text is the directory meant when no symlink is in the
# way: the $PWD candidate (short, through the symlinked cwd) goes to the
# standard.
def test_a_pwd_candidate_with_dotdot_that_folds_right(tmp: TmpDirs) -> None:
    top = long_real_dir(tmp)
    os.makedirs(top + b"/d1/d2")
    link = tmp.new() + b"/l"
    os.symlink(top, link)
    out = run_child(
        """
        import sockat.ex
        b = sockat.ex.bind("../../s.sock")
        report(spelling=type(b.spelling).__name__, route=b.route, kernel=socket.socket.getsockname(b.socket))
        """,
        cwd=link + b"/d1/d2",
    )
    assert out["spelling"] == "Original"
    assert out["route"] == "standard"
    assert out["kernel"] == os.fsdecode(link + b"/s.sock")
    assert is_socket(top + b"/s.sock")


# A short relative path in a deep cwd (neither candidate fits) goes to the
# routes, as a long path does: Linked, standard skipped, the socket at the
# path. The address reported is the absolute form (the real candidate's),
# not the relative string.
def test_a_short_relative_path_in_a_deep_cwd_goes_to_the_routes(tmp: TmpDirs) -> None:
    deep = long_real_dir(tmp)
    out = run_child(
        BIND_AND_REPORT.replace("os.chdir", "here = os.path.exists('s.sock'); report(here=here); os.chdir"), cwd=deep
    )
    assert out["spelling"] == "Linked"
    assert out["route"] == "link-dir"
    assert out["skipped"] == ["standard"]
    assert out["name"] == os.fsdecode(deep + b"/s.sock")
    assert out["here"] is True
    assert not os.path.lexists(deep + b"/s.sock")


# When no route can be used either (here: no place for B, no /proc, no thread
# cwd, no link-file), the short relative path goes to the standard as it is:
# CwdRelative, route standard, and skipped the routes found unusable without
# standard (it is the route taken). FreeBSD's at needs nothing that can be
# turned off here.
@pytest.mark.skipif(IS_FREEBSD, reason="at cannot be turned off")
def test_no_route_usable_binds_the_relative_path_as_it_is(tmp: TmpDirs) -> None:
    deep = long_real_dir(tmp)
    out = run_child(
        """
        from _support import NO_PROC, env_at
        from sockat import _calls
        from sockat._place import LinkFile
        from sockat._spelling import Trail
        trail = Trail()
        env = env_at(proc=NO_PROC, thread_cwd=False, link_file=LinkFile.NOT_TRIED)
        done, file = _calls.bind(b"s.sock", env=env, remember=True, trail=trail)
        report(
            spelling=type(done.spelling).__name__,
            route=done.route,
            skipped=[s.name for s in trail.skipped],
            kernel=os.fsdecode(socket.socket.getsockname(done.sock)),
        )
        os.chdir("/")
        file.remove()
        """,
        cwd=deep,
    )
    assert out["spelling"] == "CwdRelative"
    assert out["route"] == "standard"
    assert out["skipped"] == ["link-dir", *(["thread-cwd"] if THREAD_ROUTE_IN_BIND_ROW else [])]
    assert out["kernel"] == "s.sock"
    # remove() of a CwdRelative bind, the cwd watched: it was not changed
    # during the call, so the file was remembered and removed.
    assert not os.path.lexists(deep + b"/s.sock")


# A long relative path no route can be used for is ENAMETOOLONG (not given
# to the standard, which refuses it), for bind and resolve.
def test_no_route_usable_for_a_long_relative_path(tmp: TmpDirs) -> None:
    root = tmp.new()
    rel = os.path.relpath(long_path(root), root)
    out = run_child(
        NO_ROUTE_CODE
        + """
import errno, sockat.ex
got = []
for call in (sockat.ex.bind, sockat.ex.resolve_for_bind, sockat.ex.resolve_for_connect):
    try:
        call(ARGS["rel"])
        got.append(None)
    except OSError as e:
        got.append(errno.errorcode[e.errno])
report(got=got)
""",
        cwd=root,
        args={"rel": os.fsdecode(rel)},
    )
    assert out["got"] == ["ENAMETOOLONG"] * 3


# A bind with an absolute candidate that fails (a file at the path) raises the
# standard's error from route standard, nothing skipped, and is not tried
# again with the other candidate, a route or the relative path: the file
# stays and nothing is made in the place for B.
def test_a_failing_candidate_is_not_tried_again(tmp: TmpDirs) -> None:
    d = tmp.new()
    with open(d + b"/s.sock", "wb") as f:
        f.write(b"unrelated")
    out = run_child(
        """
        import errno, sockat.ex
        try:
            sockat.ex.bind("s.sock")
            report(error=None)
        except OSError as e:
            report(error=e.errno, route=e.route, skipped=list(e.skipped))
        """,
        cwd=d,
    )
    assert out == {**out, "error": standard_errno("bind-exists"), "route": "standard", "skipped": []}
    with open(d + b"/s.sock", "rb") as f:
        assert f.read() == b"unrelated"


# A short relative path whose directory is not there: no candidate can be
# made, and the path is looked up as for the routes: ENOENT, no route, nothing
# skipped.
def test_a_short_relative_path_without_its_directory(tmp: TmpDirs) -> None:
    out = run_child(
        """
        import sockat.ex
        try:
            sockat.ex.bind("none/s.sock")
            report(error=None)
        except OSError as e:
            report(error=type(e).__name__, route=e.route, skipped=list(e.skipped))
        """,
        cwd=tmp.new(),
    )
    assert out == {**out, "error": "FileNotFoundError", "route": None, "skipped": []}


# A short relative path that does not end with a file name is given to the
# standard as it is (no candidate without a file name): the standard's own
# error.
def test_a_short_relative_path_without_a_file_name(tmp: TmpDirs) -> None:
    d = tmp.new()
    os.mkdir(d + b"/dir")
    out = run_child(
        """
        import sockat
        for p in ("dir/", ".."):
            try:
                sockat.bind(p)
                report(**{p: None})
            except OSError as e:
                report(**{p: e.errno})
            try:
                socket.socket(socket.AF_UNIX).bind(p)
            except OSError as e:
                report(**{"standard " + p: e.errno})
        """,
        cwd=d,
    )
    assert out["dir/"] == out["standard dir/"]
    assert out[".."] == out["standard .."]


# connect does not make a relative path absolute: a short one goes to the
# standard as it is, even in a deep cwd, and nothing is made in the place for
# B.
def test_connect_keeps_a_relative_path(tmp: TmpDirs) -> None:
    deep = long_real_dir(tmp)
    with listener_at(deep + b"/s.sock"):
        out = run_child(
            """
            import sockat.ex
            c = sockat.ex.connect("s.sock")
            report(route=c.route, skipped=list(c.skipped))
            """,
            cwd=deep,
        )
    assert out["route"] == "standard"
    assert out["skipped"] == []


# resolve makes a relative path absolute as bind does: the real candidate
# (Original) for both resolves; a relative path no route can be used for is
# given back as it is (CwdRelative).
def test_resolve_makes_a_relative_path_absolute(tmp: TmpDirs) -> None:
    d = tmp.new()
    code = """
import sockat.ex
got = {}
for name, call in (("bind", sockat.ex.resolve_for_bind), ("connect", sockat.ex.resolve_for_connect)):
    r = call("s.sock")
    got[name] = [type(r.spelling).__name__, r.path, r.route]
report(**got)
"""
    want = os.fsdecode(real(d) + b"/s.sock")
    out = run_child(code, cwd=d)
    assert out["bind"] == out["connect"] == ["Original", want, "standard"]
    kept = run_child(NO_ROUTE_CODE + code, cwd=d)
    assert kept["bind"] == kept["connect"] == ["CwdRelative", "s.sock", "standard"]


# ForConnect carries the spelling's properties: Original for a short absolute
# path, Linked (not removable by the spelling) for link-file, whose until is
# Process through /proc and Cleanup through B's real path.
def test_for_connect_spelling(tmp: TmpDirs) -> None:
    short = tmp.new() + b"/s.sock"
    assert sockat.ex.resolve_for_connect(short).spelling == sockat.ex.Original()
    p = long_path(tmp.new())
    with listener_at(p):
        r = sockat.ex.resolve_for_connect(p, options=sockat.ex.Options(base=tmp.new() + b"/b"))
        try:
            assert r.route == "link-file"
            assert isinstance(r.spelling, sockat.ex.Linked)
            assert r.spelling.removable is False
            assert r.spelling.until is (sockat.ex.Until.PROCESS if HAS_PROC else sockat.ex.Until.CLEANUP)
            with socket.socket(socket.AF_UNIX) as c:
                c.connect(r.path)
        finally:
            r.cleanup()


# A long absolute path whose real candidate fits (a symlink on the way to a
# short real directory) goes to the standard as that: Original, nothing
# skipped, nothing made for B (Options.base is not even checked: B is not
# used); remove() removes the socket. The listener reports the path given
# (the kernel keeps the candidate). resolve_for_bind gives the real candidate
# back.
def test_a_long_absolute_path_whose_real_candidate_fits(tmp: TmpDirs) -> None:
    short = real(tmp.new())
    holder = deep_dir(tmp.new(), 120)
    os.symlink(short, holder + b"/l")
    p = holder + b"/l/s.sock"
    assert len(p) > DIRECT_MAX
    base = tmp.new() + b"/b"
    o = sockat.ex.Options(base=base)
    bound = sockat.ex.bind(p, options=o)
    with bound.socket:
        assert (bound.route, bound.skipped, bound.spelling) == ("standard", (), sockat.ex.Original())
        assert is_socket(short + b"/s.sock")
        assert not os.path.exists(base)
        assert bound.socket.getsockname() == os.fsdecode(p)
        assert os.fsencode(socket.socket.getsockname(bound.socket)) == short + b"/s.sock"
        bound.file.remove()
    assert not os.path.lexists(short + b"/s.sock")
    r = sockat.ex.resolve_for_bind(p, options=o)
    assert (r.path, r.spelling, r.route) == (short + b"/s.sock", sockat.ex.Original(), "standard")


# Options.base is checked only when the routes are gone to (SPEC: an absolute
# candidate given to the standard uses no B). A base that fails the checks (a
# symlink: EPERM) does not stop a long absolute path whose real candidate
# fits; it is the error of a path that goes to the routes, a short relative
# one in a deep cwd included (not taken as "no route usable": no fall back to
# the relative path as it is).
def test_options_base_is_checked_only_for_the_routes(tmp: TmpDirs) -> None:
    bad = tmp.new() + b"/b"
    os.symlink(tmp.new(), bad)
    o = sockat.ex.Options(base=bad)
    short = real(tmp.new())
    holder = deep_dir(tmp.new(), 120)
    os.symlink(short, holder + b"/l")
    bound = sockat.ex.bind(holder + b"/l/s.sock", options=o)
    with bound.socket:
        assert bound.route == "standard"
        bound.file.remove()
    with raises_errno(errno.EPERM) as e:
        sockat.ex.bind(long_path(tmp.new()), options=o)
    assert e.value.route is None  # type: ignore[attr-defined]
    out = run_child(
        """
        import sockat.ex
        try:
            sockat.ex.bind("s.sock", options=sockat.ex.Options(base=ARGS["bad"]))
            report(error=None)
        except OSError as e:
            report(error=e.errno, route=e.route)
        """,
        cwd=long_real_dir(tmp),
        args={"bad": os.fsdecode(bad)},
    )
    assert out == {**out, "error": errno.EPERM, "route": None}


DEEP_CWD_CODE = """
import errno, os, tempfile
_limit = os.pathconf("/", "PC_PATH_MAX")
_root = tempfile.mkdtemp(prefix="spt-deep-", dir="/tmp")
os.chdir(_root)
_length = len(_root)
while _length <= _limit:
    os.mkdir("d" * 200)
    os.chdir("d" * 200)
    _length += 201
def _outcome(call):
    try:
        got = call()
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
    return "ok (%d bytes)" % len(got)
seen = "PATH_MAX %d, cwd %d bytes: os.getcwdb() %s, os.path.realpath('.') %s" % (
    _limit, _length, _outcome(os.getcwdb), _outcome(lambda: os.path.realpath(".", strict=True)))
"""
"""Code that moves a child into a cwd past PATH_MAX (made a component at a
time, relative to the one before) and notes in ``seen`` what
``os.getcwdb`` and ``os.path.realpath`` give there (an errno, or success)."""


# In a cwd past PATH_MAX, a short relative path binds as the standard binds it
# (SPEC "相対パスを絶対パスにする" 5): no absolute candidate is short enough, the
# routes cannot point E at a directory that long, and the relative path goes
# to the standard as it is: route standard, CwdRelative, standard not in
# skipped (where the realpath itself could not be had for its length, nothing
# is skipped). The resolve gives the relative path back the same way. What
# os.getcwdb / os.path.realpath give there differs by system (libc, Python);
# it is printed so that a CI log (the BSDs, illumos) tells.
def test_a_short_relative_path_in_a_cwd_past_path_max(tmp: TmpDirs, capsys: pytest.CaptureFixture[str]) -> None:
    out = run_child(
        DEEP_CWD_CODE
        + """
import socket, sockat, sockat.ex
from sockat import _calls
from sockat._spelling import Trail
trail = Trail()
done, _ = _calls.bind(b"s.sock", trail=trail)
c = sockat.connect("s.sock")
r = sockat.ex.resolve_for_bind("r.sock")
report(
    seen=seen,
    route=done.route,
    spelling=type(done.spelling).__name__,
    skipped=[s.name for s in trail.skipped],
    here=os.path.exists("s.sock"),
    resolved=[r.path, type(r.spelling).__name__, r.route],
)
""",
    )
    with capsys.disabled():
        print(f"\n[deep cwd] {out['seen']}")
    assert out["route"] == "standard"
    assert out["spelling"] == "CwdRelative"
    skipped = out["skipped"]
    assert isinstance(skipped, list)
    assert "standard" not in skipped
    assert out["here"] is True
    assert out["resolved"] == ["r.sock", "CwdRelative", "standard"]


# A relative path whose socket's directory cannot be resolved for its length
# (ENAMETOOLONG, or ERANGE as macOS's realpath(3) answers in a cwd past
# PATH_MAX) is not an error: no route is tried, and the path goes on as in 5
# with nothing skipped: a short one to the standard as it is (CwdRelative),
# a long one ENAMETOOLONG with no route. Forced here with a realpath that
# fails so (Python's own resolves such a cwd).
@pytest.mark.parametrize("code", [errno.ENAMETOOLONG, errno.ERANGE])
def test_a_realpath_too_long_goes_on_without_routes(tmp: TmpDirs, code: int) -> None:
    deep = long_real_dir(tmp)
    rel = os.fsdecode(os.path.relpath(long_path(deep, total=len(deep) + 150), deep))
    assert len(rel) > DIRECT_MAX
    out = run_child(
        """
        import errno
        import sockat.ex
        real = os.path.realpath
        def refused(p, **kw):
            if not os.fsencode(p).startswith(b"/"):
                raise OSError(ARGS["code"], os.strerror(ARGS["code"]))
            return real(p, **kw)
        os.path.realpath = refused
        b = sockat.ex.resolve_for_bind("s.sock")
        try:
            sockat.ex.bind(ARGS["rel"])
            long = None
        except OSError as e:
            long = [errno.errorcode[e.errno], e.route, list(e.skipped)]
        report(short=[type(b.spelling).__name__, b.route, list(b.skipped)], long=long)
        """,
        cwd=deep,
        args={"code": code, "rel": rel},
    )
    assert out["short"] == ["CwdRelative", "standard", []]
    assert out["long"] == ["ENAMETOOLONG", None, []]
