# sockat reference

> English | [日本語](./REFERENCE-ja.md)

sockat-py's names and types in Python, the Python form of the patterns not to use, and notes specific to Python. How to use it is in the [user manual](./MANUAL.md). The language-independent behaviour (routes, how relative paths are handled, what stays in the base, clean up, what differs from the standard library for a long path, the error situations) is defined by [the sockat specification](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md), which this reference links to instead of copying. How the routes are built is in [SPEC-routes](https://github.com/kawaz/sockat/blob/main/docs/SPEC-routes.md), how far they reach in [the reach table](https://github.com/kawaz/sockat/blob/main/pages/reach/README.md), and the design decisions of the Python version in [DR-0001](./decisions/DR-0001-python-port-design.md).

## Functions

| Function | Standard library code it replaces | Returns |
|---|---|---|
| `sockat.bind(path)` | `socket()` → `bind(path)` → `listen()` | a listening `socket.socket` |
| `sockat.connect(path)` | `socket()` → `connect(path)` | `socket.socket` |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | a non-blocking `socket.socket` |
| `sockat.datagram.bind(path)` | `socket(AF_UNIX, SOCK_DGRAM)` → `bind(path)` | `socket.socket` |
| `sockat.datagram.unbound()` | `socket(AF_UNIX, SOCK_DGRAM)` | `socket.socket` |
| `sockat.datagram.connect(sock, path)` | `sock.connect(path)` | `None` |
| `sockat.ex.bind(path, options=...)` and so on | the same + spelling properties, clean up, diagnostics | `Bound` / `Connected` / `Report` ([sockat.ex](#sockatex)) |
| `sockat.ex.resolve_for_bind(path, options=...)` / `resolve_for_connect` | a short spelling you pass to the standard library yourself | `ForBind` / `ForConnect` ([resolve](#resolve)) |
| `sockat.ex.sweep()` / `sweep_base(path)` | — | `None` ([sweep](#sweep)) |

- `path` is `str` / `bytes` / `os.PathLike`
- It runs on Python 3.11 or later and depends on no other package (pure Python)
- Every socket is made with `socket.socket(AF_UNIX, SOCK_STREAM)` (`SOCK_DGRAM` for DGRAM), so it gets the same settings as the standard library (close-on-exec, the value of `socket.setdefaulttimeout`, the default listen backlog). To change the backlog, call `listen(n)` again on the returned listener
- Closing the socket does not remove the file (the same as Python's standard library). To remove it, use `file.remove()` of `sockat.ex.bind`, or `os.unlink(path)` with the original path (unlink has no `sun_path` limit)

## connect and connect_nonblocking

The behaviour is in the specification's "[Non-blocking connect](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#non-blocking-connect)". In Python:

- `connect` follows the value of `settimeout`, the same as the standard library's `connect`
- `connect_nonblocking` fails without waiting when the backlog is full, with `BlockingIOError` on Linux and `ConnectionRefusedError` on macOS

## sockat.datagram

Of the standard library's DGRAM calls, it mirrors only the functions that take a path, and `unbound`, which is used together with them ([the specification's "Functions"](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#functions)). How to use it is in [Using DGRAM](./manual/datagram.md).

- `connect` tries the routes with the socket you pass. It does not close the socket on failure (closing it is up to the user)
- What `unbound()` returns is a subclass of `socket.socket` ([Notes specific to Python](#notes-specific-to-python))
- `send_to` is not there yet (open in the specification)

## SEQPACKET

There are no SEQPACKET functions. Pass a resolve's spelling to a `socket.socket(AF_UNIX, SOCK_SEQPACKET)` you make yourself (how to use it is in [Using SEQPACKET](./manual/seqpacket.md)).

- The standard library on macOS cannot make a SEQPACKET socket (`EPROTONOSUPPORT`)
- With a long file name, `resolve_for_bind` may be `ENAMETOOLONG` even at a length the stream `sockat.bind` reaches (a resolve uses only the routes whose spelling stays)

## sockat.ex

It is not loaded by `import sockat`. The user opts in with `import sockat.ex`. Call it through the module, as in `sockat.ex.connect(...)` ([Notes specific to Python](#notes-specific-to-python)).

| Function | Returns |
|---|---|
| `sockat.ex.bind(path, *, options=None)` | `Bound(socket, file, spelling, route, base, skipped)` |
| `sockat.ex.connect(path, *, options=None)` / `connect_nonblocking` | `Connected(socket, route, skipped)` |
| `sockat.ex.datagram.bind(path, *, options=None)` | `Bound` (the socket is DGRAM) |
| `sockat.ex.datagram.connect(sock, path, *, options=None)` | `Report(route, skipped)` |
| `sockat.ex.resolve_for_bind(path, *, options=None)` | `ForBind(path, spelling, route, base, skipped)` and `cleanup()`, `socket_file()` |
| `sockat.ex.resolve_for_connect(path, *, options=None)` | `ForConnect(path, spelling, route, skipped)` and `cleanup()` |
| `sockat.ex.sweep()` / `sockat.ex.sweep_base(path)` | `None` |

- `options` can be passed only as a keyword (`sockat.ex.bind(path, options=sockat.ex.Options(...))`)
- `Bound` / `Connected` / `Report` are named tuples and can be unpacked in the order above. The meaning of each field is in the specification's "[What is returned](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#what-is-returned)" and "[Diagnostics](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#diagnostics)"

### Options

A keyword-only frozen dataclass. The meaning of each field is in the specification's "[`Options`](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#options)".

| Field | Type |
|---|---|
| `usable_cross_uid` | `bool = False` |
| `base` | `str \| bytes \| os.PathLike \| None = None`. An absolute path ending in a name (otherwise `ValueError`) |

### Spelling properties (Spelling)

`Bound.spelling` / `ForBind.spelling` / `ForConnect.spelling` is a value of `Spelling`. The meaning of the values and attributes is in the specification's "[Spelling properties: `Spelling`](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#spelling-properties-spelling)".

| Value | Form in Python |
|---|---|
| `Original` | `sockat.ex.Original()` |
| `CwdRelative` | `sockat.ex.CwdRelative()` |
| `Linked` | `sockat.ex.Linked(removable, until, usable_cross_uid, usable_with_private_tmp)`. `until` is `sockat.ex.Until.PROCESS` / `Until.CLEANUP` (a `StrEnum` whose values are `"Process"` / `"Cleanup"`) |
| `Dangling` | `sockat.ex.Dangling()` |

Each value is a frozen dataclass of its own, so a class pattern of `match` can branch on it attribute by attribute. Read an unknown value as "the spelling cannot be used".

```python
match bound.spelling:
    case sockat.ex.Linked(removable=True):
        ...  # unlinking by the spelling removes the socket file
    case sockat.ex.Original() | sockat.ex.CwdRelative():
        ...  # the path itself
    case _:
        ...  # it does not: remove by the path or file.remove()
```

### remove and cleanup

`Bound.file` is a `SocketFile`, which has `remove()` and `cleanup()`. What they do and the extent of what is promised are in the specification's "[Cleanup](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#cleanup)" (**call `remove()` before closing the listener**). In Python:

- A failure is returned as an `OSError` (the subclass for its errno). When both fail in `remove()`, the error on the file side is returned, and the error of `cleanup()` is added to that exception as a note (`add_note`)
- For `sockat.ex.bind` of a relative path given to the standard library as it is (`CwdRelative`), if the cwd differs between the start of the call and after recording, an `OSError` without errno
- The default `sockat.bind` returns no `file`. What stays, and how a long-lived process cleans up, is in the specification's "[What stays in the base](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#what-stays-in-the-base)"

### Diagnostics and errors

The meaning of `route`, `base` and `skipped` is in the specification's "[Diagnostics](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#diagnostics)". In Python:

- `skipped` is a tuple of `Skipped(name, reason)` (named tuples)
- On failure, the attributes `skipped` and `route` are added to the exception (whose type and `errno` are the same as the default calls') (it is neither wrapped nor turned into another type)

### resolve

The behaviour is in the specification's "[resolve](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#resolve)" and "[`SocketFile` of `ForBind`](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#socketfile-of-forbind)". How to use it is in [Resolving just the path](./manual/resolve.md).

- The spelling (`path`) is `str` if the path given is `str`, and `bytes` if it is `bytes`
- `ForBind` has no `remove()`. Calling `socket_file()` after binding yourself and before closing the listener returns a `SocketFile` (before the bind it is an error: `FileNotFoundError` if there is no file, an `OSError` of `ENOTSOCK` if it is not a socket)

### sweep

`sockat.ex.sweep()` and `sockat.ex.sweep_base(path)`. What they remove and what they do not is in the specification's "[`sweep`](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#sweep)". sockat never calls them on its own, so in environments that repeat tests, call `sockat.ex.sweep()`.

## Relative paths

bind and resolve make a relative path absolute before handling it, and connect passes it on as it is. The steps and what differs from the standard library are in the specification's "[Making a relative path absolute](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#making-a-relative-path-absolute)". In Python:

- When an absolute candidate goes to the standard library, a plain `socket.socket` is returned. `getsockname()` is the candidate's spelling the kernel returns (an absolute path)
- When the routes are gone to, the subclass that replaces the address is returned, and `getsockname()` returns the form of the real candidate (the realpath of the socket's directory and the file name)
- Only when no route can be used and the relative path goes to the standard library as it is (`CwdRelative`) does `getsockname()` return the relative path given
- `$PWD` is read from the environment on each call

## Notes specific to Python

- **Addresses return the original path**: for a long path, the address the kernel returns is the spelling of the bind (the specification's "[What differs from the standard library for a long path](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#what-differs-from-the-standard-library-for-a-long-path)"). The sockets sockat returns are a subclass of `socket.socket` that replaces `getsockname()` of a listener (and of an accepted socket) and `getpeername()` of a connected socket with the original path (also when a long absolute path goes to the standard as its real candidate; for a bind of a relative path, see [Relative paths](#relative-paths)). Where a standard socket fails (`EBADF` after closing, `ENOTCONN` for `getpeername` of a listener), it fails the same way. For a short path it is a plain `socket.socket` ([DR-0003](./decisions/DR-0003-report-the-given-path.md))
  - `isinstance(x, socket.socket)` is true, but for a long path `type(x) is socket.socket` is false
  - A socket made again from the fd with `socket.fromfd` and the like is of the standard library's type, so it returns the kernel's spelling
- **`sockat.datagram.unbound()` returns the subclass**: so that `getpeername()` gives the original path when it is later connected to a long path with `sockat.datagram.connect` (it is not bound, so `getsockname()` stays the kernel's value). When it is reconnected to a short path, `getpeername()` goes back to the kernel's value
- **asyncio clean up**: from Python 3.13 on, `asyncio.start_unix_server(sock=)` removes the file of the listener's `getsockname()` when the server is closed (`cleanup_socket`). sockat's listener returns the original path, so the socket file at the original path is removed, the same as for a short path ([asyncio](#asyncio))
- **`sockat.ex` is a separate module**: the `_ex` functions are placed in `sockat.ex`, which `import sockat` does not load. Importing as `from sockat.ex import connect` mixes them up with the default `sockat.connect` and the standard library's `socket.socket.connect_ex`
- **Closing the listener does not remove the file**: Python's standard library does not remove the socket file when the listener is closed. sockat does not remove it either (for a listener of `sockat.ex.bind`, even for a short path, only `file.remove()` removes it)
- **The standard library's `socket` silently cuts a path at a NUL**: `socket.bind("/tmp/a\0b")` binds at `/tmp/a` (a `str` or `bytes` alike; seen with CPython 3.11 on macOS and Linux). sockat makes a NUL anywhere but at the start an argument error (`ValueError`) at any length (a leading NUL on Linux, the abstract namespace, goes to the standard as is)
- **`os.path.abspath`, `normpath` and `relpath` fold `..` lexically**: for `a/sym/../s.sock` (sym is a symlink), the kernel takes the parent of the symlink's target, but these functions make it `a/s.sock`. To make a relative path absolute yourself, use `os.path.join(os.getcwd(), path)`. sockat itself looks up the directory with `os.stat` and `os.path.realpath` (which resolves symlinks before handling `..`), and when it folds a spelling from `$PWD` lexically, it uses it only after checking that the folded directory is the one meant, so it points to the same place as the kernel

## Patterns not to use

The language-independent list and why each is wrong are in the specification's "[Patterns not to use](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#patterns-not-to-use)". Here are the patterns in Python and the code to write instead. The code is read out of this document and run by the test (`tests/test_docs.py`). `path` is the socket's path.

### Removing by the spelling the kernel returns without looking at Spelling

- Pattern: unlinking / stat-ing the socket file by the spelling the kernel returns, as in `os.unlink(socket.socket.getsockname(listener))`
- Instead: keep the original path and use it. `file.remove()`

```python
import os

import sockat

listener = sockat.bind(path)
...
listener.close()
os.unlink(path)
```

### Using an old spelling after cleanup or after the process ends

- Pattern: connecting by a resolve's spelling (`ForConnect.path` and so on) after `cleanup()`. Using a process's bind spelling after that process ends
- Instead: use a spelling only during `until`

```python
import socket

import sockat.ex

bound = sockat.ex.bind(path)
r = sockat.ex.resolve_for_connect(path)
for _ in range(3):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(r.path)
r.cleanup()
bound.file.remove()
bound.socket.close()
```

### Binding by a relative path and removing by the relative string given after changing the cwd

- Pattern: after `sockat.bind("server.sock")`, calling `os.chdir(...)` and removing with `os.unlink("server.sock")`
- Instead: `file.remove()`. To make the path absolute yourself, do not use a function that folds `..` lexically (`os.path.abspath` and the like. [Notes specific to Python](#notes-specific-to-python))

```python
import os

import sockat.ex

bound = sockat.ex.bind(path)  # path is relative to the cwd
os.chdir("/")
...
bound.file.remove()
bound.socket.close()
```

### Counting on closing the listener to remove the socket file

- Pattern: finishing clean up with `listener.close()` alone
- Instead: `file.remove()`, or remove by the original path

```python
import sockat.ex

bound = sockat.ex.bind(path)
...
bound.file.remove()
bound.socket.close()
```

### Counting on remove() to close the listener too

- Pattern: calling only `file.remove()` and not closing the listener
- Instead: close the listener yourself

```python
import sockat.ex

bound = sockat.ex.bind(path)
...
bound.file.remove()
bound.socket.close()
```

### Calling remove() after closing the listener

- Pattern: calling `bound.file.remove()` after `bound.socket.close()`
- Instead: call it before closing the listener

```python
import sockat.ex

bound = sockat.ex.bind(path)
...
bound.file.remove()
bound.socket.close()
```

### Having a peer of another uid follow a procB spelling or a spelling whose usable_cross_uid is False

- Pattern: handing the kernel's spelling of the default bind to a peer running as another uid and having it follow it
- Instead: give `Options(usable_cross_uid=True)` and check `spelling` of the result

```python
import sockat.ex

bound = sockat.ex.bind(path, options=sockat.ex.Options(usable_cross_uid=True))
match bound.spelling:
    case sockat.ex.Linked(usable_cross_uid=True):
        ...  # a peer of another uid can follow the kernel's spelling
    case _:
        ...  # it cannot: give the peer the path itself
bound.file.remove()
bound.socket.close()
```

### Branching on the strings of route, base, skipped or reason

- Pattern: branching on the strings of `route`, `base`, `skipped` or `reason` of `sockat.ex`, as in `if bound.route == "link-file":`
- Instead: branch on `spelling`

```python
import sockat.ex

bound = sockat.ex.bind(path)
match bound.spelling:
    case sockat.ex.Linked(removable=True):
        ...  # unlinking by the spelling removes the socket file
    case _:
        ...  # it does not: remove by the path or file.remove()
bound.file.remove()
bound.socket.close()
```

## Errors

The errno for each situation is in the specification's "[Errors](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#errors)". sockat does not rewrite the OS's errors, so the OS's errno values are the same as with that OS's standard library. The types in Python:

| Situation in the specification | Type in Python |
|---|---|
| An OS error | an `OSError` with errno (the subclass for that errno, such as `FileNotFoundError`, `NotADirectoryError`, `ConnectionRefusedError`, `BlockingIOError`, `PermissionError`) |
| A wrong argument (an empty path, a NUL other than at the start, a long path not ending in a name, a malformed `Options.base`) | `ValueError` |
| A general error (the cwd changed during relative, or during `sockat.ex.bind` of a relative path given to the standard library as it is) | an `OSError` without errno |

## asyncio

What is returned is the standard library's `socket.socket`, so it can be passed as it is to APIs that take `sock=` (how to use it is in [Listening](./manual/bind.md) and [Connecting](./manual/connect.md)).

- Works the same as for a short path without extra arguments. From Python 3.13 on, asyncio removes the socket file at the original path when the server is closed. asyncio of 3.11 / 3.12 does not remove it (the same as the standard library)
- The transport's `get_extra_info('sockname')` (server side) / `get_extra_info('peername')` (client side) are also the original path
- The `socket` of `sockat.ex` can be passed the same way. Call `file.remove()` before closing the server (while it is running)
- `connect_nonblocking` does not wait for the peer, so it can be called directly from the event loop. `bind` does not wait for a peer either (with thread-cwd it waits for the thread to end, but that ends right after the bind)

## Routes per OS

For a long path, the routes are tried in the following order, and the first route that can be used binds / connects. The names and contents of the routes are in the specification's "[Route names](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#route-names)", and how far they reach is in [the reach table](https://github.com/kawaz/sockat/blob/main/pages/reach/README.md). sockat-py builds every route in the specification (it does not fork).

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` usable) | link-dir (procB → B's real path) → thread-cwd | proc-direct → thread-cwd-direct → link-file |
| Linux / Android (`/proc` not usable) | link-dir (B's real path) → thread-cwd | relative → thread-cwd-direct → link-file |
| macOS / iOS | link-dir (B's real path) → link-file → thread-cwd | relative → widened → thread-cwd-direct → link-file |
| FreeBSD | link-dir (B's real path) → at | at-direct → link-file |
| NetBSD | link-dir (B's real path) → link-file | relative → link-file |
| OpenBSD / illumos | link-dir (B's real path) | relative → link-file |
| Other Unix | link-dir (B's real path) → link-file (whether it is followed is told at run time) | relative → link-file |
| Windows | not supported (`ImportError` on import) | same |

## What is not confirmed

- It has not been run on Android and iOS
- FreeBSD, NetBSD, OpenBSD and illumos run the tests on CI VMs
- Language-independent open points are in the specification's "[Open questions](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#open-questions)"
