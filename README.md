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
| `sockat.connect(path)` | `socket()` → `connect(path)` | a `socket.socket` |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | a nonblocking `socket.socket` |
| `sockat.ex.bind(path)` and the like | the same + what was lost, cleaning up the socket file, the last resort | a `Bound` / `Connected` ([below](#calls-with-cleaning-up-and-diagnosis-sockatex)) |

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
| symlink-dir | `sockat.ex.bind` with the last resort only. Create a short symlink to the socket's directory in a place, go through it, and remove it at once ([below](#calls-with-cleaning-up-and-diagnosis-sockatex)) |
| at-direct | connects only. Pass the fd of the socket's directory and the file name to `connectat(2)` (FreeBSD) |
| fork-cwd, fork-cwd-sockdir, fork-cwd-direct | **none** (the specification's fork routes are not built. The last resort of `sockat.ex.connect`, fork-cwd-direct, only appears in `skipped`) |

- None of the routes changes the cwd of the calling process or thread, so they can be used from multi-threaded programs
- A connect tries the routes that need neither a write (creating D, R or a symlink) nor a thread (proc-direct, relative, widened, at-direct) first, and the routes that create D or start a thread after them. D is created at most once per connect and shared by the routes that use it (thread-cwd, at, proc, proc-held). When it can be created nowhere, every route that uses it is skipped
- On macOS a bind reaches a path through symlink-file whatever the length of its file name (losing only `respelled`)
- A bind through the routes through D reaches a path at any directory depth as long as the file name is within 96 bytes (92 on macOS / FreeBSD, about 79 with proc), shorter than the standard library's limit by `R/`. When D can be created nowhere, the form without D reaches up to 88 bytes (84 on macOS / FreeBSD, about 72 with proc-sockdir)
- On NetBSD / OpenBSD / illumos (when their bind does not follow symlink-file's link), and where no means of the routes through D can be used (Linux without `/proc` under a seccomp filter, and the like), a `bind` of a long path gets `ENAMETOOLONG` (where the specification reaches it through the fork-cwd route, which Python does not have; ruled by kawaz in the hub's D12-Q3). To reach it even there, use `sockat.ex.bind` with the last resort ([below](#calls-with-cleaning-up-and-diagnosis-sockatex))
- A path that fits no route gets `ENAMETOOLONG` (an `OSError` with errno), as a path too long for the standard library does
- The conditions of each route, and where a path stops being reached, are in [docs/MANUAL.md](./docs/MANUAL.md#how-a-long-path-is-reached)

## What differs from the standard library for a long path

- `getsockname()` of a listener (and of an accepted socket) and `getpeername()` of a connected socket return the original path. What the kernel returns is `R/<file name>` (`/proc/self/fd/<fd>/R/<file name>` with proc, R being `.sockat.XXXXXXXXXX` in the form without D, `<place>/sockat.XXXXXXXXXX` with symlink-file; R and the symlink are gone, so it names nothing), but the sockets sockat returns replace it with the original path
- A path too long for the standard library that does not end with a name (ending in `dir/`, `dir/.`, `..`) is a `ValueError`
- A path containing a NUL anywhere but at its start is a `ValueError` at any length (the standard library silently truncates at the NUL). On Linux a path starting with a NUL (abstract namespace) is handed to the standard library unchanged
- In a socket's directory that is not sticky and that others can write to, R of the form without D can be replaced between its creation and the bind, so the bind may go to another directory (a name already there gives `EADDRINUSE`; nothing is overwritten). R inside D cannot be replaced, D being 0700
- If the process dies midway, `sockat.XXXXXXXXXX` may be left in a place (`TMPDIR`, `/tmp`, and for symlink-dir also `XDG_RUNTIME_DIR`), and `.sockat.XXXXXXXXXX` in the socket's directory
- The rest (the directory being looked up first, and the like) is in [docs/MANUAL.md](./docs/MANUAL.md#what-differs-from-the-standard-library-for-a-long-path)

## Calls with cleaning up and diagnosis (sockat.ex)

```python
import sockat.ex

listener, discard, losses, route, skipped = sockat.ex.bind(path)
...
listener.close()
discard()

sock, losses, route, skipped = sockat.ex.connect(path)
```

- `sockat.ex.bind` / `sockat.ex.connect` / `sockat.ex.connect_nonblocking` try the same routes as the plain `sockat.bind` / `sockat.connect` / `sockat.connect_nonblocking`, and also return the name of the route taken (`route`), the routes tried and found unusable (`skipped`, with `name` and `reason`), and what was lost (`losses`)
- Call them through the module, as `sockat.ex.connect(...)` (imported as `from sockat.ex import connect`, they are easily mistaken for the plain `sockat.connect`)
- `import sockat` does not load `sockat.ex`. Whoever uses it chooses it with `import sockat.ex`
- `discard()` removes the socket file the bind made, and nothing else (not when it has been replaced by another file). It may be called any number of times, and it does not close the listener (the caller does)
- `losses` is what the route taken lost (`Loss`'s `respelled`, `recreatable-spelling`, `cwd-relative`; empty when nothing was lost). Branch on it. `route` and `skipped` are for diagnosis, and their contents are not promised
- `last_resort=True` uses the last resort: `sockat.ex.bind` tries symlink-dir last where the plain bind gets `ENAMETOOLONG` (no route can be used) (it creates in a place `sockat.XXXXXXXXXX` pointing to the socket's directory, binds with `<place>/sockat.XXXXXXXXXX/<file name>`, and removes it at once; the places are `XDG_RUNTIME_DIR`, `TMPDIR` and `/tmp`, those only the owner can write to first, the shared ones after; through a shared place it loses `recreatable-spelling`). A connect's last resort is the fork route, which Python does not have, so it only appears in `skipped`
- When a call fails, the error (of the same type and `errno` as the plain call's) gets the attributes `skipped` and `route`
- Details are in [docs/MANUAL.md](./docs/MANUAL.md#sockatex)

## asyncio

```python
listener = sockat.bind(path)
server = await asyncio.start_unix_server(handle, sock=listener)

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- It works as with a short path, with no extra arguments (from Python 3.13, the socket file at the original path is removed when the server is closed)
- The `listener` / `sock` from `sockat.ex` can be passed the same way

## Documentation

- [docs/MANUAL.md](./docs/MANUAL.md) — usage, the routes in detail, what differs from the standard library, errors
- [docs/decisions/](./docs/decisions/INDEX.md) — design decisions (DRs, in Japanese)

## License

MIT License, Yoshiaki Kawazu (@kawaz)
