# sockat

> English | [日本語](./README-ja.md)

A Python library that binds and connects Unix domain sockets even when the path is longer than `sun_path` allows (104 bytes on macOS and the BSDs, 108 on Linux, NUL included).

The language-independent behaviour is defined by the [sockat specification](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md); sockat-py is its Python implementation.

```python
import sockat

listener = sockat.bind("/a/very/deep/directory/.../server.sock")
client = sockat.connect("/a/very/deep/directory/.../server.sock")
```

## Usage

```sh
pip install sockat
```

Use `sockat.bind` / `sockat.connect` instead of creating a `socket.socket(AF_UNIX, SOCK_STREAM)` and calling `bind` + `listen` / `connect`. In an event loop such as asyncio, use `sockat.connect_nonblocking`, which does not wait and returns a nonblocking socket.

| Function | Standard library code it replaces | Returns |
|---|---|---|
| `sockat.bind(path)` | `socket()` → `bind(path)` → `listen()` | a listening `socket.socket` |
| `sockat.bind_ex(path)` | the same + cleaning up the socket file | `(listener, discard)` ([below](#bind-with-cleanup)) |
| `sockat.connect(path)` | `socket()` → `connect(path)` | a `socket.socket` |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | a nonblocking `socket.socket` |
| `sockat.any.bind_any(path)` | the same as `bind` + where it does not reach, reaching it even by losing something | an `AnyBinding` ([below](#bind-that-reaches-even-by-losing-something)) |

- The result is the standard library's own `socket.socket` (for a long path, a subclass of `socket.socket` that reports the address as the original path)
- A path that the standard library accepts is handed to it unchanged, so it behaves exactly as with the standard library
- Even for a long path, the socket file is created at the path given
- `path` is a `str` / `bytes` / `os.PathLike`
- Python 3.11 or later. No dependencies (pure Python)

## Why

The standard library's `socket.bind` / `socket.connect` refuse a path that does not fit `sun_path` with `OSError('AF_UNIX path too long')`. The file system has no such limit, but deep temporary directories hit it quickly (macOS's `$TMPDIR` alone is about 50 bytes, and test harnesses and sandboxes nest further). The limit applies to the argument passed to `bind(2)` / `connect(2)`, so sockat hands the kernel a short spelling that names the same place.

## How a long path is reached

A path that the standard library accepts (up to `sun_path` − 1 bytes) is handed to it unchanged (the route standard).

| OS | Handed to the standard library |
|---|---|
| Linux / Android / illumos | up to 107 bytes |
| macOS / iOS / FreeBSD / NetBSD / OpenBSD | up to 103 bytes |

A longer path is tried on the routes below, in this order per OS, and bound / connected on the first route that can be used. Routes are called by [the specification's route names](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#route-names).

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` usable) | thread-cwd → proc | proc-direct → thread-cwd → proc → proc-held → symlink-file |
| Linux / Android (`/proc` not usable) | thread-cwd | relative → thread-cwd → symlink-file |
| FreeBSD | symlink-file → at | at-direct → at |
| macOS / iOS | symlink-file → thread-cwd | relative → widened → thread-cwd → symlink-file |
| NetBSD / OpenBSD / illumos / other Unix | symlink-file | relative → symlink-file |
| Windows | not supported (`ImportError` on import) | the same |

| Route | How |
|---|---|
| the routes through D (thread-cwd, at, proc) | create a private directory D and hold it by its fd, create inside it R, a randomly named symlink to the socket's directory, pass `R/<file name>` relative to D, then remove R and D. D is created in the first of `TMPDIR`, `/tmp` and the socket's directory where it can be |
| thread-cwd | a route through D. Only a throwaway thread moves its cwd into D (Linux / Android, macOS / iOS) |
| at | a route through D. D's fd is passed to `bindat(2)` / `connectat(2)` (FreeBSD) |
| proc | a route through D. Pass `/proc/self/fd/<fd of D>/R/<file name>` (Linux / Android) |
| thread-cwd-sockdir, at-sockdir, proc-sockdir | binds only. The form without D, when D can be created nowhere: create R (`.sockat.XXXXXXXXXX`) in the socket's directory, pass `.sockat.XXXXXXXXXX/<file name>` relative to the socket's directory with the same means, then remove R |
| proc-direct | connects only. Pass `/proc/self/fd/<fd of the socket's directory>/<file name>` |
| proc-held | connects only. Create a symlink `s` to the socket file inside D, and pass `/proc/self/fd/<fd of D>/s` |
| relative | connects only. Pass the path relative to the cwd. Only the string is rewritten; the cwd is never changed |
| widened | connects only. Pass a sockaddr carrying up to 253 bytes, which the macOS kernel accepts |
| symlink-file | create a short symlink to the socket file under `TMPDIR` or `/tmp` where no other user can replace it, go through it, and remove it at once. The spelling holds no file name. A bind goes through it only where a bind follows a symlink in the last component (confirmed on macOS; Linux / Android do not follow it, so it is not tried there), and goes on to the next route where it is not followed |
| symlink-dir | `bind_any` only. Create a short symlink to the socket's directory in a place, go through it, and remove it at once ([below](#bind-that-reaches-even-by-losing-something)) |
| at-direct | connects only. Pass the fd of the socket's directory and the file name to `connectat(2)` (FreeBSD) |
| fork-cwd, fork-cwd-sockdir, fork-cwd-direct | **none** (the specification's fork routes are not built. Neither are the specification's `connect_any` variants) |

- None of the routes changes the cwd of the calling process or thread, so they can be used from multi-threaded programs
- A connect tries the routes that need neither a write (creating D, R or a symlink) nor a thread (proc-direct, relative, widened, at-direct) first, and the routes that create D or start a thread after them. D is created at most once per connect and shared by the routes that use it (thread-cwd, at, proc, proc-held). When it can be created nowhere, every route that uses it is skipped
- On macOS a bind reaches a path through symlink-file whatever the length of its file name (losing only `respelled`)
- A bind through the routes through D reaches a path at any directory depth as long as the file name is within 96 bytes (92 on macOS / FreeBSD, about 79 with proc), shorter than the standard library's limit by `R/`. When D can be created nowhere, the form without D reaches up to 88 bytes (84 on macOS / FreeBSD, about 72 with proc-sockdir)
- On NetBSD / OpenBSD / illumos (when their bind does not follow symlink-file's link), and where no means of the routes through D can be used (Linux without `/proc` under a seccomp filter, and the like), a `bind` of a long path gets `ENAMETOOLONG` (where the specification reaches it through the fork-cwd route, which Python does not have; ruled by kawaz in the hub's D12-Q3). To reach it even there, use `bind_any` ([below](#bind-that-reaches-even-by-losing-something))
- A path that fits no route gets `ENAMETOOLONG` (an `OSError` with errno), as a path too long for the standard library does
- The conditions of each route, and where a path stops being reached, are in [docs/MANUAL.md](./docs/MANUAL.md#how-a-long-path-is-reached)

## What differs from the standard library for a long path

- `getsockname()` of a listener (and of an accepted socket) and `getpeername()` of a connected socket return the original path. What the kernel returns is `R/<file name>` (`/proc/self/fd/<fd>/R/<file name>` with proc, R being `.sockat.XXXXXXXXXX` in the form without D, `<place>/sockat.XXXXXXXXXX` with symlink-file; R and the symlink are gone, so it names nothing), but the sockets sockat returns replace it with the original path
- A path too long for the standard library that does not end with a name (ending in `dir/`, `dir/.`, `..`) is a `ValueError`
- A path containing a NUL anywhere but at its start is a `ValueError` at any length (the standard library silently truncates at the NUL). On Linux a path starting with a NUL (abstract namespace) is handed to the standard library unchanged
- In a socket's directory that is not sticky and that others can write to, R of the form without D can be replaced between its creation and the bind, so the bind may go to another directory (a name already there gives `EADDRINUSE`; nothing is overwritten). R inside D cannot be replaced, D being 0700
- If the process dies midway, `sockat.XXXXXXXXXX` may be left in a place (`TMPDIR`, `/tmp`, and for `bind_any` also `XDG_RUNTIME_DIR`), and `.sockat.XXXXXXXXXX` in the socket's directory
- The rest (the directory being looked up first, and the like) is in [docs/MANUAL.md](./docs/MANUAL.md#what-differs-from-the-standard-library-for-a-long-path)

## Bind with cleanup

```python
listener, discard = sockat.bind_ex(path)
...
listener.close()
discard()
```

- `discard()` removes only the socket file the bind made (not a file that has replaced it). It may be called any number of times
- `discard()` does not close the listener (the caller closes it; the hub's DR-0014)
- Details are in [docs/MANUAL.md](./docs/MANUAL.md#bind_ex)

## Bind that reaches even by losing something

```python
from sockat.any import bind_any

listener, losses, route, skipped = bind_any(path)
```

- It tries the same routes as `bind`, and where `bind` gets `ENAMETOOLONG` (no route can be used), it tries last symlink-dir: it creates in a place `sockat.XXXXXXXXXX` pointing to the socket's directory, binds with `<place>/sockat.XXXXXXXXXX/<file name>`, and removes it at once
- The candidate places are `XDG_RUNTIME_DIR`, `TMPDIR` (each when an absolute path), then `/tmp`. All the places only the owner can write to (owned by the calling user, no write permission for group and others) are tried first, then the shared ones (sticky, owned by root or the calling user)
- `losses` is what the route taken lost (`Loss`'s `respelled`, `recreatable-spelling`, `cwd-relative`; empty when nothing was lost). Through a shared place it includes `recreatable-spelling`: after the symlink is removed, other users can recreate the spelling the kernel keeps. Remove the socket file by the original path, not by the spelling (the address the kernel reports)
- `route` is the name of the route taken, `skipped` the routes tried that could not be used (`name` and `reason`), in order. Both are for diagnosis, and their contents are not promised. When it fails, the error (of the same type and `errno` as from `bind`) gets the attributes `skipped` and `route`
- `import sockat` does not load `sockat.any`. Whoever uses it chooses it with `from sockat.any import bind_any`
- Details are in [docs/MANUAL.md](./docs/MANUAL.md#bind_any)

## asyncio

```python
listener = sockat.bind(path)
server = await asyncio.start_unix_server(handle, sock=listener)

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- It works as with a short path, with no extra arguments (from Python 3.13, the socket file at the original path is removed when the server is closed)
- The listeners from `bind_ex` and `bind_any` can be passed the same way

## Documentation

- [docs/MANUAL.md](./docs/MANUAL.md) — usage, the routes in detail, what differs from the standard library, errors
- [docs/decisions/](./docs/decisions/INDEX.md) — design decisions (DRs, in Japanese)

## License

MIT License, Yoshiaki Kawazu (@kawaz)
