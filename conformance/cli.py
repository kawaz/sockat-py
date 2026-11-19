"""The test CLI the hub's conformance test (kawaz/sockat
``docs/design/conformance.md``) starts once per case. Not part of the
package: it sits outside ``src/`` so neither the wheel nor the sdist carries
it.

Reads one JSON object from stdin, ``{"op": "bind" | "connect", "path":
"...", "last_resort": true | false}`` (``last_resort`` may be left out: false),
makes one bind (and listen) / connect through the routes of the default bind
/ connect, or with ``last_resort`` through those of ``sockat.ex`` with the
last resort (a bind's symlink-dir last; a connect's fork-cwd-direct, which
this implementation does not have, noted as skipped), and writes one JSON
object to stdout::

    {"ok": true, "error": null, "route": "thread-cwd", "losses": ["respelled"], "skipped": ["standard"]}

* ``error``: the errno's name (``ENOENT``, ...), ``invalid-argument`` for a
  path the library refuses as an argument error (``ValueError``), ``general``
  for an :class:`OSError` without an errno;
* ``route``: the route that carried the call, or, on failure, the one whose
  bind / connect failed (``null`` when none got that far);
* ``losses``: the SPEC's names of what the route lost (``[]`` on failure);
* ``skipped``: the names of the routes tried and found unusable, in order.

A bound listener is closed (its socket file is left where it is); a
connected stream sends one byte ``x`` and is closed. The cwd and the
environment are used as given.

Start it with the project's environment and the cwd the case wants:
``just -f <repo>/justfile conformance-cli`` (``<repo>/.venv/bin/python
<repo>/conformance/cli.py``, after ``uv sync``). ``uv run`` works too, but
leaves its ``uv-*.lock`` in ``$TMPDIR``.

Exit status: 0 when a result was written (whether the call went through or
not), 2 when the input is not such an object.
"""

from __future__ import annotations

import errno
import json
import sys
from dataclasses import dataclass
from typing import Literal

from sockat._route import Loss, Reached, Request, Route, Trail, reach
from sockat._sys import Op


@dataclass(frozen=True)
class Case:
    """The input: what to do, the path handed to the library as is, and
    whether the last resort is used."""

    op: Literal["bind", "connect"]
    path: str
    last_resort: bool = False


class BadInput(Exception):
    """The input is not ``{"op": "bind" | "connect", "path": "<str>",
    "last_resort": <bool>}`` (``last_resort`` optional)."""


def parse(text: str) -> Case:
    """The input's :class:`Case`, or :class:`BadInput`."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise BadInput(f"not JSON: {e}") from e
    if not isinstance(doc, dict):
        raise BadInput("not a JSON object")
    op = doc.get("op")
    if op != "bind" and op != "connect":
        raise BadInput('"op" must be "bind" or "connect"')
    path = doc.get("path")
    if not isinstance(path, str):
        raise BadInput('"path" must be a string')
    last_resort = doc.get("last_resort", False)
    if not isinstance(last_resort, bool):
        raise BadInput('"last_resort" must be true or false')
    return Case(op, path, last_resort)


def _names(skipped: list[tuple[Route, str]] | tuple[tuple[Route, str], ...]) -> list[str]:
    return [route.value for route, _ in skipped]


def _losses(losses: tuple[Loss, ...]) -> list[str]:
    return [loss.value for loss in losses]


def run(case: Case) -> dict[str, object]:
    """Make the call and give the output object."""
    trail = Trail()
    req = Request(Op.BIND if case.op == "bind" else Op.CONNECT, last_resort=case.last_resort)
    try:
        done: Reached = reach(req, case.path, trail)
    except ValueError:
        return _failed("invalid-argument", trail)
    except OSError as e:
        name = errno.errorcode.get(e.errno, "general") if e.errno is not None else "general"
        return _failed(name, trail)
    with done.sock as sock:
        if case.op == "connect":
            sock.sendall(b"x")
    return {
        "ok": True,
        "error": None,
        "route": done.route.value,
        "losses": _losses(done.losses),
        "skipped": _names(done.skipped),
    }


def _failed(error: str, trail: Trail) -> dict[str, object]:
    return {
        "ok": False,
        "error": error,
        "route": None if trail.trying is None else trail.trying.value,
        "losses": [],
        "skipped": _names(trail.skipped),
    }


def main() -> int:
    try:
        case = parse(sys.stdin.read())
    except BadInput as e:
        print(f"conformance/cli.py: {e}", file=sys.stderr)
        return 2
    json.dump(run(case), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
