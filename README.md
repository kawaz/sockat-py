# sockat

> English | [日本語](./README-ja.md)

A Python library that binds and connects Unix domain sockets even at paths past the `sun_path` limit (104 bytes including the NUL on macOS and the BSDs, 108 bytes on Linux).

The language-independent behaviour is defined by [the sockat specification](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md); sockat-py is its Python implementation.

```python
import sockat

listener = sockat.bind("/a/very/deep/directory/.../server.sock")
client = sockat.connect("/a/very/deep/directory/.../server.sock")
```

## Usage

```sh
pip install sockat
```

Instead of making a `socket.socket(AF_UNIX, ...)` and calling `bind` (+ `listen`) / `connect`, use sockat's functions. In an event loop such as asyncio, use `sockat.connect_nonblocking`, which returns a non-blocking socket without waiting.

| Function | Standard code it replaces | Returns |
|---|---|---|
| `sockat.bind(path)` | `socket()` → `bind(path)` → `listen()` | a listening `socket.socket` |
| `sockat.connect(path)` | `socket()` → `connect(path)` | `socket.socket` |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | a non-blocking `socket.socket` |
| `sockat.datagram.bind(path)` / `unbound()` / `connect(sock, path)` | the same code with `SOCK_DGRAM` | `socket.socket` / `None` |
| `sockat.ex.bind(path, options)` and so on | the same + spelling properties, clean up, diagnostics | `Bound` / `Connected` / `Report` ([below](#calls-with-clean-up-and-diagnostics-sockatex)) |
| `sockat.ex.resolve_for_bind` / `resolve_for_connect` | a short spelling you pass to the standard library yourself | `ForBind` / `ForConnect` |

- What is returned is the standard `socket.socket` (for a long path, a subclass of `socket.socket` that returns the original path as its address)
- A path of a length the standard library accepts is passed to it as it is, so it behaves the same as the standard library
- Even for a long path, the socket file is made at the path you gave
- `path` is `str` / `bytes` / `os.PathLike`
- Python 3.11 or later. No dependencies (pure Python)

## Why it is needed

The standard `socket.bind` / `socket.connect` refuse a path that does not fit in `sun_path` with `OSError('AF_UNIX path too long')`. The file system has no such limit, yet putting a socket in a deep temporary directory (macOS's `$TMPDIR` alone is around 50 bytes, and test frameworks and sandboxes dig deeper) hits it quickly. The limit applies to the length of the argument passed to `bind(2)` / `connect(2)`, so sockat passes the kernel a short spelling that points to the same place.

## How long paths are reached

A path of a length the standard library accepts (`sun_path` − 1: 107 bytes on Linux / Android / illumos, 103 bytes on macOS / iOS / the BSDs) is passed to it as it is (route standard). For a longer path, the routes are tried in the following order per OS, and the first route that can be used binds / connects. Routes are called by [the names in the specification](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#route-names).

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

| Route | How |
|---|---|
| link-dir | In a per-process directory D inside the place's B (`/tmp/sockat-<uid>` and so on), make a symlink E pointing to the socket's directory, and bind with `B/D/E/<file name>`. B is spelled as `/proc/<pid>/fd/<B's fd>` (procB) or as B's real path. E stays until cleaned up, so the spelling keeps pointing to the original path |
| link-file | Go through an E pointing to the socket file. The file name does not appear in the spelling, so it reaches regardless of the file name's length. Bind only on OSes that follow a symlink in the last component (confirmed on macOS and NetBSD). The default connect goes through an E directly under B and removes it at once |
| thread-cwd | Move the cwd of a throwaway thread only to D, bind with `E/<file name>` through a random E in D, and remove it at once (Linux / Android, macOS / iOS) |
| at | Pass the same spelling as thread-cwd to `bindat(2)` with D's fd (FreeBSD) |
| proc-direct | Connect only. `/proc/self/fd/<fd of the socket's directory>/<file name>` |
| relative | Connect only. A path relative to the cwd (only the string is rewritten; the cwd is not changed) |
| widened | Connect only. A sockaddr that holds up to 253 bytes, which the macOS kernel accepts |
| at-direct | Connect only. The fd of the socket's directory and the file name to `connectat(2)` (FreeBSD) |
| thread-cwd-direct | Connect only. Move the cwd of a throwaway thread only to the socket's directory and pass the file name |

- No route changes the cwd of the calling process or thread, so it can be used from multithreaded programs. It does not fork
- Connect tries first the routes that need neither writes nor threads, and leaves nothing behind
- A path that fits in no route fails with `ENAMETOOLONG` (an `OSError` with errno), the same as a path too long for the standard library
- The places B, D, E and the conditions and reach of each route are in [docs/MANUAL.md](./docs/MANUAL.md#how-long-paths-are-reached)

## What differs from the standard for a long path

- `getsockname()` of a listener (and of an accepted socket) and `getpeername()` of a connected socket return the original path. The kernel returns the spelling of the bind, but the sockets sockat returns replace it with the original path
- State grows outside the app's own place: B, D and E remain in `/tmp`, `$XDG_RUNTIME_DIR` and `$TMPDIR`, and the E of the default `bind` and the D of a dead process remain until the OS cleans its temporary directories (`sockat.ex.sweep()` removes the D of dead processes). A B made in the socket's directory because B could be made nowhere else (`.sockat.XXXXXXXXXX`), and a B made beside one that failed the check (`sockat-<uid>.XXXXXXXXXX`), may remain, removed neither by `sweep()` nor by the OS's clean up
- Do not use a spelling after it is cleaned up (a B in `/tmp` may be made again by someone else after it is gone)
- If the peer's `/tmp` is isolated (systemd's `PrivateTmp` and so on), the peer cannot follow a spelling through `/tmp`
- On Linux older than systemd v242, and when B is in macOS's `$TMPDIR`, a live D may be removed (the spelling only becomes unfollowable)
- A path of a length that cannot be passed to the standard library as it is and that does not end in a name (ending in `dir/`, `dir/.` or `..`) raises `ValueError`
- A path that contains a NUL other than at the start raises `ValueError` regardless of its length (the standard library silently truncates at the NUL). On Linux a path starting with a NUL (abstract namespace) is passed to the standard library as it is
- Others (the directory is looked up first, and so on) are in [docs/MANUAL.md](./docs/MANUAL.md#what-differs-from-the-standard-for-a-long-path)

## Calls with clean up and diagnostics (sockat.ex)

```python
import sockat.ex

bound = sockat.ex.bind(path)
...
bound.file.remove()
bound.socket.close()

connected = sockat.ex.connect(path)
```

- `sockat.ex.bind` / `connect` / `connect_nonblocking` (and `sockat.ex.datagram.bind` / `connect`) try the same routes as the default calls and return, along with the result, the spelling's properties (`spelling`), the route taken (`route`), the B taken (`base`) and the routes tried that could not be used (`skipped`)
- Call them through the module, as in `sockat.ex.connect(...)`. `sockat.ex` is not loaded by `import sockat`
- `Options(usable_cross_uid=True)` asks for a spelling a peer of another uid can also follow (bind calls only), and `Options(base=...)` lets the app give the location of B (`sockat.ex.sweep_base(path)` removes the dead D in that B)
- `spelling` is one of `Original` / `CwdRelative` / `Linked(removable, until, usable_cross_uid, usable_with_private_tmp)` / `Dangling`. Branch on it. `route`, `base` and `skipped` are for diagnostics and their contents are not promised
- `file.remove()` removes only the socket file the bind made (if it has been replaced by another file while the listener is open, it does not remove it) and also cleans up the spelling's E. `file.cleanup()` cleans up only E. Each works once, and neither closes the listener. Call `remove()` before closing the listener
- `resolve_for_bind` / `resolve_for_connect` return a short spelling you pass to the standard library yourself (for reusing a spelling when connecting to the same path repeatedly, and so on). After binding to the spelling of `resolve_for_bind` yourself, you can get a `SocketFile` with `socket_file()` and `remove()` it
- On failure, the exception (whose type and `errno` are the same as the default calls') carries `skipped` and `route` attributes
- Details are in [docs/MANUAL.md](./docs/MANUAL.md#sockatex)

## Patterns not to use

Patterns that remove unrelated files or fail to remove files. The details and the code to write instead are in [docs/MANUAL.md](./docs/MANUAL.md#patterns-not-to-use) (the code is run as a test).

| Pattern | Instead |
|---|---|
| `os.unlink(socket.socket.getsockname(listener))` (removing by the spelling the kernel returns without looking at `spelling`) | `os.unlink(path)` (the original path) or `file.remove()` |
| Using an old spelling after `cleanup()` or after the process ends | Use a spelling only during `until` |
| `sockat.bind("server.sock")` → `os.chdir(...)` → `os.unlink("server.sock")` | `os.path.abspath(path)` before the bind. `file.remove()` |
| Finishing clean up with `listener.close()` alone | `file.remove()` or `os.unlink(path)` |
| Calling only `file.remove()` and not closing the listener | `close()` the listener too |
| Calling `file.remove()` after closing the listener | Call it before closing |
| Having a peer of another uid follow the spelling of the default bind | Give `Options(usable_cross_uid=True)` and check `spelling` |
| `if bound.route == "link-file":` (branching on `route`, `base`, `skipped` or `reason`) | `match bound.spelling:` (branching on `spelling`) |

## asyncio

```python
listener = sockat.bind(path)
server = await asyncio.start_unix_server(handle, sock=listener)

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- Works the same as for a short path without extra arguments (from Python 3.13 on, the socket file at the original path is removed when the server is closed)
- The `socket` of `sockat.ex` can be passed the same way

## Documentation

- [docs/MANUAL.md](./docs/MANUAL.md) — usage, route details, differences from the standard library, errors
- [docs/decisions/](./docs/decisions/INDEX.md) — design decisions (DRs)

## License

MIT License, Yoshiaki Kawazu (@kawaz)
