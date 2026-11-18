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

- The result is the standard library's own `socket.socket` (for a long path, a subclass of `socket.socket` that reports the address as the original path)
- A path that the standard library accepts is handed to it unchanged, so it behaves exactly as with the standard library
- Even for a long path, the socket file is created at the path given
- `path` is a `str` / `bytes` / `os.PathLike`
- Python 3.11 or later. No dependencies (pure Python)

## Why

The standard library's `socket.bind` / `socket.connect` refuse a path that does not fit `sun_path` with `OSError('AF_UNIX path too long')`. The file system has no such limit, but deep temporary directories hit it quickly (macOS's `$TMPDIR` alone is about 50 bytes, and test harnesses and sandboxes nest further). The limit applies to the argument passed to `bind(2)` / `connect(2)`, so sockat hands the kernel a short spelling that names the same place.

## How a long path is reached

A path that the standard library accepts (up to `sun_path` − 1 bytes) is handed to it unchanged.

| OS | Handed to the standard library |
|---|---|
| Linux / Android / illumos | up to 107 bytes |
| macOS / iOS / FreeBSD / NetBSD / OpenBSD | up to 103 bytes |

A longer path is tried on the routes below, in this order per OS, and bound / connected on the first route that can be used.

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` usable) | thread cwd → /proc variant | thread cwd → /proc variant → /proc → fd-held form → symlink |
| Linux / Android (`/proc` not usable) | thread cwd | thread cwd → relative path → symlink |
| FreeBSD | bindat | connectat → connectat (socket's directory) |
| macOS / iOS | thread cwd | thread cwd → relative path → widened sockaddr → symlink |
| NetBSD / OpenBSD / illumos / other Unix | none (`ENAMETOOLONG`) | relative path → symlink |
| Windows | not supported (`ImportError` on import) | the same |

| Route | How |
|---|---|
| central route | create a private directory D and hold it by its fd, create inside it R, a randomly named symlink to the socket's directory, pass `R/<file name>` relative to D, then remove R and D. D is created in the first of `TMPDIR`, `/tmp` and the socket's directory where it can be |
| thread cwd | a means of the central route. Only a throwaway thread moves its cwd into D (Linux / Android, macOS / iOS) |
| bindat / connectat | a means of the central route. D's fd is passed to `bindat(2)` / `connectat(2)` (FreeBSD) |
| /proc variant | a means of the central route. Pass `/proc/self/fd/<fd of D>/R/<file name>` (Linux / Android) |
| /proc | connects only. Pass `/proc/self/fd/<fd of the socket's directory>/<file name>` |
| fd-held form | connects only. Create a symlink `s` to the socket file inside D, and pass `/proc/self/fd/<fd of D>/s` |
| relative path | connects only. Pass the path relative to the cwd. Only the string is rewritten; the cwd is never changed |
| widened sockaddr | connects only. Pass a sockaddr carrying up to 253 bytes, which the macOS kernel accepts |
| symlink | connects only. Create a short symlink to the socket file under `TMPDIR` or `/tmp` where no other user can replace it, go through it, and remove it at once |
| connectat (socket's directory) | connects only. Pass the fd of the socket's directory and the file name to `connectat(2)` (FreeBSD) |
| fork | **none** (the specification's fork route is not built. Neither are the specification's `connect_any` variants) |

- None of the routes changes the cwd of the calling process or thread, so they can be used from multi-threaded programs
- A bind reaches a path at any directory depth as long as the file name is within 96 bytes (92 on macOS / FreeBSD, about 79 with the /proc variant), shorter than the standard library's limit by `R/`
- On NetBSD / OpenBSD / illumos, and where no means of the central route can be used (Linux without `/proc` under a seccomp filter, and the like), a bind of a long path gets `ENAMETOOLONG` (where the specification reaches it through the fork route; awaiting a ruling in the hub's D12-Q3)
- A path that fits no route gets `ENAMETOOLONG` (an `OSError` with errno), as a path too long for the standard library does
- The conditions of each route, and where a path stops being reached, are in [docs/MANUAL.md](./docs/MANUAL.md#how-a-long-path-is-reached)

## What differs from the standard library for a long path

- `getsockname()` of a listener (and of an accepted socket) and `getpeername()` of a connected socket return the original path. What the kernel returns is `R/<file name>` (`/proc/self/fd/<fd>/R/<file name>` with the /proc variant; R is gone, so it names nothing), but the sockets sockat returns replace it with the original path
- A path too long for the standard library that does not end with a name (ending in `dir/`, `dir/.`, `..`) is a `ValueError`
- A path containing a NUL anywhere but at its start is a `ValueError` at any length (the standard library silently truncates at the NUL). On Linux a path starting with a NUL (abstract namespace) is handed to the standard library unchanged
- If the process dies midway, `sockat.XXXXXXXXXX` may be left under `TMPDIR` or `/tmp`, and `.sockat.XXXXXXXXXX` in the socket's directory
- The rest (the directory being looked up first, and the like) is in [docs/MANUAL.md](./docs/MANUAL.md#what-differs-from-the-standard-library-for-a-long-path)

## Bind with cleanup

```python
listener, discard = sockat.bind_ex(path)
...
listener.close()
discard()
```

- `discard()` removes only the socket file the bind made (not a file that has replaced it). It may be called any number of times
- `discard()` does not close the listener
- The name is provisional. Details are in [docs/MANUAL.md](./docs/MANUAL.md#bind_ex)

## asyncio

```python
listener = sockat.bind(path)
server = await asyncio.start_unix_server(handle, sock=listener)

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- It works as with a short path, with no extra arguments (from Python 3.13, the socket file at the original path is removed when the server is closed)

## Documentation

- [docs/MANUAL.md](./docs/MANUAL.md) — usage, the routes in detail, what differs from the standard library, errors
- [docs/decisions/](./docs/decisions/INDEX.md) — design decisions (DRs, in Japanese)

## License

MIT License, Yoshiaki Kawazu (@kawaz)
