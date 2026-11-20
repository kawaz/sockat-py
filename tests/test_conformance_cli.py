"""The test CLI of the hub's conformance test (``conformance/cli.py``, hub
``docs/design/conformance.md``): its input, its output for a bind and a
connect, the errors' names, and the exit status."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from types import ModuleType

import pytest
from _support import PRIVATE_PLACE_CODE, SRC_DIR, TESTS_DIR, TmpDirs, listener_at, long_path

CLI = TESTS_DIR.parent / "conformance" / "cli.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sockat_conformance_cli", CLI)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Registered before it runs: its dataclass looks its module up there.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_cli(doc: object, cwd: bytes = b"/") -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    out = subprocess.run(
        # The CLI run as it is, after the code that gives it the session's
        # own place for B (not the shared /tmp/sockat-<uid>).
        [sys.executable, "-c", PRIVATE_PLACE_CODE + f"import runpy; runpy.run_path({str(CLI)!r}, run_name='__main__')"],
        input=doc if isinstance(doc, str) else json.dumps(doc),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )
    return out.returncode, out.stdout


# Input that is not the object conformance.md describes exits 2 with nothing
# on stdout.
@pytest.mark.parametrize(
    "doc",
    [
        "not json",
        "[]",
        {"op": "listen", "path": "x"},
        {"op": "bind"},
        {"op": "bind", "path": "x", "usable_cross_uid": "yes"},
        {"op": "bind", "path": "x", "base": 1},
    ],
)
def test_bad_input_exits_2(doc: object) -> None:
    code, stdout = run_cli(doc)
    assert code == 2
    assert stdout == ""


# A long bind: one JSON line with ok, route, base, the Spelling as JSON and
# the names of the routes skipped; the listener is left open and the file
# stays.
def test_a_long_bind(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    code, stdout = run_cli({"op": "bind", "path": os.fsdecode(p), "base": os.fsdecode(tmp.new() + b"/b")})
    assert code == 0
    out = json.loads(stdout)
    assert out["ok"] is True
    assert out["error"] is None
    assert out["route"] == "link-dir"
    assert out["base"] == "given"
    assert out["spelling"]["kind"] == "Linked"
    assert set(out["spelling"]) == {"kind", "removable", "until", "usable_cross_uid", "usable_with_private_tmp"}
    assert out["skipped"] == ["standard"]
    assert os.path.exists(p)


# A short bind reports Original / CwdRelative and no base.
def test_a_short_bind(tmp: TmpDirs) -> None:
    d = tmp.new()
    code, stdout = run_cli({"op": "bind", "path": "s.sock"}, cwd=d)
    assert code == 0
    assert json.loads(stdout) == {
        "ok": True,
        "error": None,
        "route": "standard",
        "base": None,
        "spelling": {"kind": "CwdRelative"},
        "skipped": [],
    }


# A connect sends x and reports no base and no spelling.
def test_a_long_connect(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    with listener_at(p) as listener:
        code, stdout = run_cli({"op": "connect", "path": os.fsdecode(p)})
        assert code == 0
        out = json.loads(stdout)
        assert out["ok"] is True
        assert out["base"] is None
        assert out["spelling"] is None
        conn, _ = listener.accept()
        with conn:
            assert conn.recv(1) == b"x"


# Failures: the errno's name, invalid-argument for an argument error, the
# route that failed, and no base or spelling.
def test_failures(tmp: TmpDirs) -> None:
    p = long_path(tmp.new())
    code, stdout = run_cli({"op": "connect", "path": os.fsdecode(p)})
    out = json.loads(stdout)
    assert (code, out["ok"], out["error"], out["spelling"], out["base"]) == (0, False, "ENOENT", None, None)
    assert out["route"] is not None
    code, stdout = run_cli({"op": "bind", "path": os.fsdecode(p) + "/"})
    out = json.loads(stdout)
    assert (out["error"], out["route"], out["skipped"]) == ("invalid-argument", None, [])


# The names of errnos: the C name, the ones with two names made one, an
# unnamed one as errno-<n>, an OSError without errno "general".
def test_error_names() -> None:
    import errno

    cli = load()
    assert cli.error_name(OSError(errno.ENOENT, "x")) == "ENOENT"
    assert cli.error_name(OSError(errno.EAGAIN, "x")) == "EAGAIN"
    assert cli.error_name(OSError(errno.EOPNOTSUPP, "x")) == "EOPNOTSUPP"
    assert cli.error_name(OSError(9999, "x")) == "errno-9999"
    assert cli.error_name(OSError("no errno")) == "general"
    assert cli.error_name(ValueError("x")) == "invalid-argument"
