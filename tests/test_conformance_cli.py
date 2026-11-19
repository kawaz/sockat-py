"""The test CLI of the hub's conformance test (``conformance/cli.py``): the
shape of its output on the first cases of the hub's ``docs/design/conformance.md``
(the bind / connect of a short and a long path, a missing directory, a file
in the middle, a file already at the path, no listener, a path that does not
end with a file name or holds a NUL), and the input's ``last_resort``.

What the hub's script checks after the CLI ends is checked here too: the
socket file at the path given, nothing left in ``$TMPDIR`` or the socket's
directory, and the ``x`` a connect sends."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _support import (
    HAS_PROC,
    NO_PROC,
    SRC_DIR,
    TmpDirs,
    bind_route_here,
    is_socket,
    listener_at,
    needs_long_bind,
    nest,
    stale_socket,
    thread_cwd_usable,
)

from sockat import _route
from sockat._route import Config
from sockat._sys import DIRECT_MAX, IS_APPLE, IS_FREEBSD, IS_LINUX

CLI = Path(__file__).resolve().parents[1] / "conformance" / "cli.py"

LONG = DIRECT_MAX + 40
"""A socket path length past what the ``socket`` module takes."""


def cli_env() -> dict[str, str]:
    """The environment to start the CLI with: this one, with the source put
    first on ``PYTHONPATH``, made absolute. The CI's VMs set
    ``PYTHONPATH=src``, relative to the project, which the CLI started in
    another cwd would not find."""
    paths = [str(SRC_DIR), *filter(None, [os.environ.get("PYTHONPATH")])]
    return {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}


def cli(case: object, *, cwd: bytes, tmpdir: bytes) -> dict[str, object]:
    """Run the CLI on ``case`` (as JSON on stdin) in ``cwd`` with ``$TMPDIR``
    set to ``tmpdir``, and give its output object (it must exit 0 and write
    exactly the five keys)."""
    env = {**cli_env(), "TMPDIR": os.fsdecode(tmpdir)}
    done = subprocess.run(
        [sys.executable, str(CLI)],
        input=json.dumps(case),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert isinstance(out, dict)
    assert list(out) == ["ok", "error", "route", "losses", "skipped"]
    return out


def leftovers(*dirs: bytes) -> list[bytes]:
    """What the routes would have left: ``sockat.*`` in the places,
    ``.sockat.*`` in the socket's directory."""
    return [n for d in dirs for n in os.listdir(d) if n.startswith((b"sockat.", b".sockat."))]


def test_short_absolute_bind(tmp: TmpDirs) -> None:
    root, places = tmp.new(), tmp.new()
    path = root + b"/s.sock"
    out = cli({"op": "bind", "path": os.fsdecode(path)}, cwd=root, tmpdir=places)
    assert out == {"ok": True, "error": None, "route": "standard", "losses": [], "skipped": []}
    assert is_socket(path)


def test_short_relative_bind(tmp: TmpDirs) -> None:
    root, places = tmp.new(), tmp.new()
    out = cli({"op": "bind", "path": "s.sock"}, cwd=root, tmpdir=places)
    assert out == {"ok": True, "error": None, "route": "standard", "losses": ["cwd-relative"], "skipped": []}
    assert is_socket(root + b"/s.sock")


# Where no route of the default bind reaches a long path (OpenBSD and
# illumos, whose bind does not follow symlink-file's link and which have no
# means of the routes through D), the long bind is ENAMETOOLONG, which the
# routes' own tests check; the shape of a long bind's output is checked
# where one goes through.
@needs_long_bind
def test_long_absolute_bind(tmp: TmpDirs) -> None:
    root, places = tmp.new(), tmp.new()
    path = nest(root, LONG)
    out = cli({"op": "bind", "path": os.fsdecode(path)}, cwd=root, tmpdir=places)
    assert out["ok"] is True
    assert out["error"] is None
    assert out["route"] not in (None, "standard")
    assert out["losses"] == ["respelled"]
    skipped = out["skipped"]
    assert isinstance(skipped, list)
    assert skipped[:1] == ["standard"]
    assert is_socket(path)
    assert leftovers(places, os.path.dirname(path)) == []


@pytest.mark.parametrize("length", [40, LONG], ids=["short", "long"])
def test_connect_sends_x(tmp: TmpDirs, length: int) -> None:
    root, places = tmp.new(), tmp.new()
    path = nest(root, length) if length > DIRECT_MAX else root + b"/s.sock"
    with listener_at(path) as listener:
        out = cli({"op": "connect", "path": os.fsdecode(path)}, cwd=root, tmpdir=places)
        assert out["ok"] is True
        assert out["error"] is None
        assert out["losses"] == []
        if length <= DIRECT_MAX:
            assert out == {"ok": True, "error": None, "route": "standard", "losses": [], "skipped": []}
        else:
            # Which routes before it are unusable depends on the system (a
            # seccomp filter refusing unshare skips thread-cwd), so only the
            # first is fixed.
            assert out["route"] not in (None, "standard")
            skipped = out["skipped"]
            assert isinstance(skipped, list)
            assert skipped[:1] == ["standard"]
            assert out["route"] not in skipped
        listener.settimeout(10)
        conn, _ = listener.accept()
        with conn:
            conn.settimeout(10)
            assert conn.recv(2) == b"x"
            assert conn.recv(1) == b""
    assert leftovers(places, os.path.dirname(path)) == []


def test_long_missing_directory(tmp: TmpDirs) -> None:
    root, places = tmp.new(), tmp.new()
    path = nest(root, LONG)
    missing = os.path.dirname(path) + b"/missing/s.sock"
    out = cli({"op": "bind", "path": os.fsdecode(missing)}, cwd=root, tmpdir=places)
    assert out == {"ok": False, "error": "ENOENT", "route": None, "losses": [], "skipped": []}


def test_long_file_in_the_middle(tmp: TmpDirs) -> None:
    root, places = tmp.new(), tmp.new()
    Path(os.fsdecode(root + b"/file")).touch()
    path = root + b"/file/" + b"d" * (LONG - len(root) - len(b"/file/") - len(b"/s.sock")) + b"/s.sock"
    assert len(path) == LONG
    out = cli({"op": "bind", "path": os.fsdecode(path)}, cwd=root, tmpdir=places)
    assert out == {"ok": False, "error": "ENOTDIR", "route": None, "losses": [], "skipped": []}


# Where no route of the default bind can bind a long path (OpenBSD and
# illumos), the bind ends in ENAMETOOLONG before any route binds, file or no
# file at the path (SPEC: the long bind's end), which the routes' own tests
# check; EADDRINUSE from a route is checked where one binds.
@needs_long_bind
def test_long_bind_onto_a_file(tmp: TmpDirs) -> None:
    root, places = tmp.new(), tmp.new()
    path = nest(root, LONG)
    Path(os.fsdecode(path)).touch()
    out = cli({"op": "bind", "path": os.fsdecode(path)}, cwd=root, tmpdir=places)
    assert out["ok"] is False
    assert out["error"] == "EADDRINUSE"
    assert out["route"] not in (None, "standard")
    assert out["losses"] == []
    assert leftovers(places, os.path.dirname(path)) == []


# A connect to a socket nobody listens on fails on the first route of the
# connect order that connects at all (with the path too long for relative
# from the cwd: proc-direct where /proc can be used, the widened sockaddr on
# Apple, at-direct on FreeBSD, the thread's own cwd or else the symlink
# elsewhere), and `route` names that route, the routes before it in the order
# being `skipped` (hub SPEC: the route whose connect failed).
def test_long_connect_without_listener(tmp: TmpDirs) -> None:
    root, places = tmp.new(), tmp.new()
    path = nest(root, LONG)
    stale_socket(path)
    out = cli({"op": "connect", "path": os.fsdecode(path)}, cwd=root, tmpdir=places)
    assert out["ok"] is False
    assert out["error"] == "ECONNREFUSED"
    assert out["losses"] == []
    order = [r.value for r in _route.connect_order(HAS_PROC)]
    if IS_FREEBSD:
        failed = "at-direct"
    elif HAS_PROC:
        failed = "proc-direct"
    elif IS_APPLE:
        failed = "widened"
    elif IS_LINUX and thread_cwd_usable():
        failed = "thread-cwd"
    else:
        failed = "symlink-file"
    assert out["route"] == failed
    assert out["skipped"] == ["standard", *order[: order.index(failed)]]
    assert leftovers(places, os.path.dirname(path)) == []


@pytest.mark.parametrize("tail", [b"/", b"/.", b"/..", b"/a\0b"], ids=["slash", "dot", "dotdot", "nul"])
def test_long_invalid_argument(tmp: TmpDirs, tail: bytes) -> None:
    root, places = tmp.new(), tmp.new()
    directory = os.path.dirname(nest(root, LONG))
    before = sorted(os.listdir(directory))
    out = cli({"op": "bind", "path": os.fsdecode(directory + tail)}, cwd=root, tmpdir=places)
    assert out == {"ok": False, "error": "invalid-argument", "route": None, "losses": [], "skipped": []}
    assert sorted(os.listdir(directory)) == before
    assert os.listdir(places) == []


@pytest.mark.parametrize(
    "case",
    [
        "[]",
        '{"op": "listen", "path": "s"}',
        '{"op": "bind"}',
        '{"op": "bind", "path": 1}',
        "not json",
        '{"op": "bind", "path": "s", "last_resort": 1}',
        '{"op": "connect", "path": "s", "last_resort": "true"}',
        '{"op": "bind", "path": "s", "last_resort": null}',
    ],
)
def test_bad_input(tmp: TmpDirs, case: str) -> None:
    root = tmp.new()
    done = subprocess.run(
        [sys.executable, str(CLI)],
        input=case,
        capture_output=True,
        text=True,
        cwd=root,
        env=cli_env(),
        check=False,
        timeout=60,
    )
    assert done.returncode == 2
    assert done.stdout == ""
    assert done.stderr.startswith("conformance/cli.py: ")
    assert os.listdir(root) == []


# ----- last_resort -----


# `last_resort` may be left out, meaning false (the hub's conformance.md):
# the same call with it left out, false and true gives the same output where
# a route of the default order carries it (the last resort is only tried
# past the end of that order), here a long connect to a listener and a long
# bind.
@pytest.mark.parametrize("op", ["connect", "bind"])
def test_last_resort_left_out_false_and_true_where_the_default_carries_it(tmp: TmpDirs, op: str) -> None:
    if op == "bind" and bind_route_here() is None:
        pytest.skip("no route of the default bind for a long path here")
    outs = []
    for extra in ({}, {"last_resort": False}, {"last_resort": True}):
        root, places = tmp.new(), tmp.new()
        path = nest(root, LONG)
        case = {"op": op, "path": os.fsdecode(path), **extra}
        if op == "connect":
            with listener_at(path) as listener:
                out = cli(case, cwd=root, tmpdir=places)
                listener.settimeout(10)
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(10)
                    assert conn.recv(2) == b"x"
        else:
            out = cli(case, cwd=root, tmpdir=places)
            assert is_socket(path)
        assert out["ok"] is True, (extra, out)
        assert leftovers(places, os.path.dirname(path)) == []
        outs.append(out)
    assert outs[0] == outs[1] == outs[2], outs


def load_cli() -> ModuleType:
    """The CLI as a module, loaded in this process (it is no package), to
    run a case with the configuration the environment would give replaced
    (:func:`run` reads it through ``Config.from_env``)."""
    spec = importlib.util.spec_from_file_location("sockat_conformance_cli", CLI)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs, as an import would: a dataclass in the CLI
    # looks its module up in sys.modules while the class is made (Python
    # 3.11 fails with the module missing there).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Where no route of the default order carries the call, `last_resort` true
# runs the order of sockat.ex with the last resort, false the default one
# (the hub's conformance.md): run in this process with the environment's
# configuration replaced by one where nothing of the default order can be
# used (no place for D or symlink-file, no thread cwd, /proc made to look
# absent).
#
# * connect: false is ENAMETOOLONG with no route and every route of the
#   order skipped; true skips fork-cwd-direct last (sockat-py has no fork
#   route; the conformance "lacks" it) and is ENAMETOOLONG all the same;
# * bind: false is ENAMETOOLONG; true goes through symlink-dir in a place of
#   our own, losing respelled only. FreeBSD's bindat cannot be turned off,
#   so the bind is not stood in for there.
def test_last_resort_true_uses_the_last_resort(tmp: TmpDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load_cli()
    own = tmp.new()
    nothing = Config(
        own_dir_places=(),
        link_places=(),
        bind_link_places=(own,),
        wide=False,
        proc_self_fd=NO_PROC,
        thread_cwd=False,
        bind_symlink_file=False,
    )
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: nothing))
    far = nest(tmp.new(), 600, b"n" * (DIRECT_MAX + 3))
    order = [r.value for r in _route.connect_order(False)]
    for last_resort, tail in ((False, []), (True, ["fork-cwd-direct"])):
        out = mod.run(mod.Case("connect", os.fsdecode(far), last_resort))
        assert out == {
            "ok": False,
            "error": "ENAMETOOLONG",
            "route": None,
            "losses": [],
            "skipped": ["standard", *order, *tail],
        }, (last_resort, out)
    if IS_FREEBSD:
        return
    p = nest(tmp.new(), 600)
    out = mod.run(mod.Case("bind", os.fsdecode(p), False))
    assert (out["ok"], out["error"], out["route"]) == (False, "ENAMETOOLONG", None), out
    out = mod.run(mod.Case("bind", os.fsdecode(p), True))
    assert (out["ok"], out["route"], out["losses"]) == (True, "symlink-dir", ["respelled"]), out
    assert is_socket(p)
    assert os.listdir(own) == []
