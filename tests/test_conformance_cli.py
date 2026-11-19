"""The test CLI of the hub's conformance test (``conformance/cli.py``): the
shape of its output on the first cases of the hub's ``docs/design/conformance.md``
(the bind / connect of a short and a long path, a missing directory, a file
in the middle, a file already at the path, no listener, a path that does not
end with a file name or holds a NUL).

What the hub's script checks after the CLI ends is checked here too: the
socket file at the path given, nothing left in ``$TMPDIR`` or the socket's
directory, and the ``x`` a connect sends."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _support import HAS_PROC, TmpDirs, is_socket, nest, thread_cwd_usable

import sockat
from sockat import _route
from sockat._sys import DIRECT_MAX, IS_APPLE, IS_FREEBSD, IS_LINUX

CLI = Path(__file__).resolve().parents[1] / "conformance" / "cli.py"

LONG = DIRECT_MAX + 40
"""A socket path length past what the ``socket`` module takes."""


def cli(case: object, *, cwd: bytes, tmpdir: bytes) -> dict[str, object]:
    """Run the CLI on ``case`` (as JSON on stdin) in ``cwd`` with ``$TMPDIR``
    set to ``tmpdir``, and give its output object (it must exit 0 and write
    exactly the five keys)."""
    env = {**os.environ, "TMPDIR": os.fsdecode(tmpdir)}
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
    with sockat.bind(path) as listener:
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
    sockat.bind(path).close()
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
    ["[]", '{"op": "listen", "path": "s"}', '{"op": "bind"}', '{"op": "bind", "path": 1}', "not json"],
)
def test_bad_input(tmp: TmpDirs, case: str) -> None:
    root = tmp.new()
    done = subprocess.run(
        [sys.executable, str(CLI)], input=case, capture_output=True, text=True, cwd=root, check=False, timeout=60
    )
    assert done.returncode == 2
    assert done.stdout == ""
    assert done.stderr.startswith("conformance/cli.py: ")
    assert os.listdir(root) == []
