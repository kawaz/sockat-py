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

- The results are the standard library's own `socket.socket`
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

| OS | Order tried |
|---|---|
| Linux / Android (`/proc` usable) | /proc |
| Linux / Android (`/proc` not usable) | relative path → symlink → thread cwd |
| FreeBSD | bindat |
| macOS / iOS | relative path → widened sockaddr (connect only) → symlink → thread cwd |
| NetBSD / OpenBSD / illumos / other Unix | relative path → symlink |
| Windows | not supported (`ImportError` on import) |

| Route | How |
|---|---|
| /proc | open the directory and pass `/proc/self/fd/<fd>/<file name>` |
| bindat | `bindat(2)` / `connectat(2)`, by the file name relative to the directory fd |
| relative path | pass the path relative to the cwd. Only the string is rewritten; the cwd is never changed |
| widened sockaddr | pass a sockaddr carrying up to 253 bytes, which the macOS kernel accepts. Used by connects only (on a bind, the standard library's `getsockname` would read past the listener's address) |
| symlink | create a short symlink under `TMPDIR` or `/tmp`, go through it, and remove it at once. It is created only in a place where no other user can replace it. A connect on Linux with `/proc` also uses a place that is not safe, through a private directory created there and held by its fd |
| thread cwd | move the cwd of a throwaway thread alone into the directory and pass the file name. The process's cwd is never changed |
| fork | **none** (the specification's fork route is not built. Neither are the specification's `connect_any` variants) |

- None of the routes changes the calling process's cwd, so they can be used from multi-threaded programs
- A path that fits no route gets `ENAMETOOLONG` (an `OSError` with errno), as a path too long for the standard library does
- On macOS / Linux, where the thread cwd route can be used, a path is reached at any depth as long as the file name is within 103 bytes (107 on Linux; 253 for a connect on macOS). Linux cannot use the thread cwd route under a seccomp filter (docker's default, for example)
- The length limits of each route, and where a path stops being reached, are in [docs/MANUAL.md](./docs/MANUAL.md#how-a-long-path-is-reached)

## What differs from the standard library for a long path

- `getsockname()` / `getpeername()` do not return the original path: they return the spelling handed to the kernel at bind time (a relative path, `/proc/self/fd/...`, a path through a symlink that no longer exists, the file name alone, ...). Keep the original path yourself
- A path too long for the standard library that does not end with a name (ending in `dir/`, `dir/.`, `..`) is a `ValueError`
- A path containing a NUL anywhere but at its start is a `ValueError` at any length (the standard library silently truncates at the NUL). On Linux a path starting with a NUL (abstract namespace) is handed to the standard library unchanged
- The rest (the directory being looked up first, a symlink that may be left behind) is in [docs/MANUAL.md](./docs/MANUAL.md#what-differs-from-the-standard-library-for-a-long-path)

## asyncio

```python
listener = sockat.bind(path)
server = await asyncio.start_unix_server(handle, sock=listener, cleanup_socket=False)  # Python 3.13 and later

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

On Python 3.13 and later, asyncio tries to remove the socket file through the listener's `getsockname()` when it closes. For a long path that spelling is not the path, so pass `cleanup_socket=False` and remove the socket file yourself ([details](./docs/MANUAL.md#asyncio)).

## Documentation

- [docs/MANUAL.md](./docs/MANUAL.md) — usage, the routes in detail, what differs from the standard library, errors
- [docs/decisions/](./docs/decisions/INDEX.md) — design decisions (DRs, in Japanese)

## License

MIT License, Yoshiaki Kawazu (@kawaz)
