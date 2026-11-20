"""The test CLI the hub's conformance test (kawaz/sockat
``docs/design/conformance.md``) starts once per case. Not part of the
package: it sits outside ``src/`` so neither the wheel nor the sdist carries
it.

Reads one JSON object from stdin::

    {"op": "bind" | "connect", "path": "...", "usable_cross_uid": false, "base": null}

(``usable_cross_uid`` may be left out: false; ``base`` left out or null: not
given), makes one bind (and listen) / connect with ``sockat.ex.bind`` /
``sockat.ex.connect`` and those ``Options``, and writes one JSON object to
stdout::

    {"ok": true, "error": null, "route": "link-dir", "base": "proc",
     "spelling": {"kind": "Linked", "removable": true, "until": "Process",
                  "usable_cross_uid": false, "usable_with_private_tmp": true},
     "skipped": ["standard"]}

* ``error``: the errno's name (``ENOENT``, ...; ``EAGAIN``, ``EOPNOTSUPP``
  and ``EDEADLK`` for the values with two names; ``errno-<n>`` for one with
  no name), ``invalid-argument`` for a path the library refuses as an
  argument error (``ValueError``), ``general`` for an :class:`OSError`
  without an errno;
* ``route``: the route that carried the call, or, on failure, the one whose
  bind / connect failed (``null`` when none got that far);
* ``base``: the bind's B (``null`` when the spelling goes through none, for
  a connect and on failure);
* ``spelling``: the bind's ``Spelling`` (``null`` for a connect and on
  failure);
* ``skipped``: the names of the routes tried and found unusable, in order.

Neither ``remove()`` nor ``cleanup()`` is called. A bound listener is left
open until the process ends; a connected stream sends one byte ``x`` and is
closed. The cwd and the environment are used as given.

Exit status: 0 when a result was written (whether the call went through or
not), 2 when the input is not such an object.
"""

from __future__ import annotations

import errno
import json
import sys
from dataclasses import dataclass
from typing import Literal

import sockat.ex

_TWO_NAMES = {errno.EAGAIN: "EAGAIN", errno.EOPNOTSUPP: "EOPNOTSUPP", errno.EDEADLK: "EDEADLK"}


@dataclass(frozen=True)
class Case:
    """The input: what to do, the path handed to the library as is, and the
    ``Options``."""

    op: Literal["bind", "connect"]
    path: str
    usable_cross_uid: bool = False
    base: str | None = None


class BadInput(Exception):
    """The input is not ``{"op": "bind" | "connect", "path": "<str>",
    "usable_cross_uid": <bool>, "base": <str> | null}`` (the last two
    optional)."""


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
    cross = doc.get("usable_cross_uid", False)
    if not isinstance(cross, bool):
        raise BadInput('"usable_cross_uid" must be true or false')
    base = doc.get("base")
    if base is not None and not isinstance(base, str):
        raise BadInput('"base" must be a string or null')
    return Case(op, path, cross, base)


def error_name(e: OSError | ValueError) -> str:
    """The output's ``error`` for ``e``."""
    if isinstance(e, ValueError):
        return "invalid-argument"
    if e.errno is None:
        return "general"
    return _TWO_NAMES.get(e.errno) or errno.errorcode.get(e.errno) or f"errno-{e.errno}"


def spelling_json(s: sockat.ex.Spelling) -> dict[str, object]:
    """The SPEC's ``Spelling`` as the conformance test writes it."""
    if isinstance(s, sockat.ex.Linked):
        return {
            "kind": "Linked",
            "removable": s.removable,
            "until": s.until.value,
            "usable_cross_uid": s.usable_cross_uid,
            "usable_with_private_tmp": s.usable_with_private_tmp,
        }
    return {"kind": type(s).__name__}


def run(case: Case) -> dict[str, object]:
    """Make the call and give the output object."""
    options = sockat.ex.Options(usable_cross_uid=case.usable_cross_uid, base=case.base)
    try:
        if case.op == "bind":
            bound = sockat.ex.bind(case.path, options=options)
        else:
            connected = sockat.ex.connect(case.path, options=options)
    except (OSError, ValueError) as e:
        return {
            "ok": False,
            "error": error_name(e),
            "route": getattr(e, "route", None),
            "base": None,
            "spelling": None,
            "skipped": [s.name for s in getattr(e, "skipped", ())],
        }
    if case.op == "bind":
        # Left open until the process ends (conformance.md: the CLI does not
        # close the listener).
        _KEEP.append(bound.socket)
        return {
            "ok": True,
            "error": None,
            "route": bound.route,
            "base": bound.base,
            "spelling": spelling_json(bound.spelling),
            "skipped": [s.name for s in bound.skipped],
        }
    with connected.socket as sock:
        try:
            sock.sendall(b"x")
        except OSError as e:
            print(f"conformance/cli.py: sending x: {e}", file=sys.stderr)
    return {
        "ok": True,
        "error": None,
        "route": connected.route,
        "base": None,
        "spelling": None,
        "skipped": [s.name for s in connected.skipped],
    }


_KEEP: list[object] = []


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
