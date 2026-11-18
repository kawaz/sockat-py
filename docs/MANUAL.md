# sockat manual

> English | [日本語](./MANUAL-ja.md)

A reference for using sockat-py: how it reaches a long path, and how it differs from the standard library for long paths. The language-independent behaviour is defined by the [sockat specification](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md); the design decisions of the Python version are in [DR-0001](./decisions/DR-0001-python-port-design.md) (in Japanese).

## Usage

```python
import sockat

path = "/a/very/deep/directory/.../server.sock"
listener = sockat.bind(path)
client = sockat.connect(path)
server, _ = listener.accept()
client.sendall(b"ping")
assert server.recv(4) == b"ping"
```

| Function | Standard library code it replaces | Returns |
|---|---|---|
| `sockat.bind(path)` | `socket()` → `bind(path)` → `listen()` | a listening `socket.socket` (blocking) |
| `sockat.connect(path)` | `socket()` → `connect(path)` | a `socket.socket` (blocking) |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | a nonblocking `socket.socket` |

- For a path the standard library accepts, the result is a plain `socket.socket`. For a long path, it is a subclass of `socket.socket` that reports the address as the original path ([below](#the-address-is-reported-as-the-original-path))

- `path` is a `str` / `bytes` / `os.PathLike` (the standard library's `bind` / `connect` do not take an `os.PathLike`, but sockat does)
- A path that the standard library accepts ([the length per OS](#how-a-long-path-is-reached)) goes to the standard library unchanged
- Whatever the route, the socket is created as `socket.socket(AF_UNIX, SOCK_STREAM)`, so it is set up like the standard library's (close-on-exec, the value of `socket.setdefaulttimeout`, the default listen backlog)
- The socket file is created at the path given. It is not removed when the socket is closed (as with the standard library). To remove it, call `os.unlink(path)` with the original path (unlink has no `sun_path` limit)
- To change the backlog, call `listen(n)` again on the returned listener

## connect and connect_nonblocking

- **`connect`** blocks. As with the standard library's `connect`, it follows the value of `settimeout`
- **`connect_nonblocking`** is for event loops. The socket is made nonblocking before the connect and returned that way. When the peer's backlog is full it fails at once (`BlockingIOError` on Linux, `ConnectionRefusedError` on macOS). A Unix-domain connect completes or fails within the call, so there is nothing to wait for afterwards

## How a long path is reached

A path up to the length the standard library accepts (`sun_path` − 1) is handed to it unchanged.

| OS | Handed to the standard library |
|---|---|
| Linux / Android / illumos | up to 107 bytes |
| macOS / iOS / FreeBSD / NetBSD / OpenBSD | up to 103 bytes |
| other Unix | up to 103 bytes |

A longer path is tried on the routes below, in this order per OS, and bound / connected on the first route that can be used. None of the routes changes the calling process's cwd.

| OS | Order tried |
|---|---|
| Linux / Android (`/proc` usable) | [/proc](#proc) |
| Linux / Android (`/proc` not usable) | [relative path](#relative-path) → [symlink](#symlink) → [thread cwd](#thread-cwd) |
| FreeBSD | [bindat](#bindat) |
| macOS / iOS | [relative path](#relative-path) → [widened sockaddr](#widened-sockaddr) (connect only) → [symlink](#symlink) → [thread cwd](#thread-cwd) |
| NetBSD / OpenBSD / illumos / other Unix | [relative path](#relative-path) → [symlink](#symlink) |

The specification's last route (fork) is not built. A child forked from Python runs Python, so it cannot satisfy the specification's requirement that the child's span be async-signal-safe ([DR-0001](./decisions/DR-0001-python-port-design.md)). A path that no route in the order reaches gets `ENAMETOOLONG`.

### The routes

#### /proc

Linux / Android. The directory is opened `O_PATH` and `/proc/self/fd/<fd>/<file name>` is passed to the standard library.

- The directory part has no length limit. The whole spelling must be at most 107 bytes, so the file name can be about 90 bytes
- When `/proc/self/fd/<fd>` cannot be looked up (a chroot or a minimal container with no `/proc`), or when the file name does not fit, the routes for when `/proc` is not usable are tried in order

#### bindat

FreeBSD. `bindat(2)` / `connectat(2)` are called through ctypes, passing the file name relative to the directory's fd. The file name can be up to 103 bytes. The directory is opened `O_RDONLY`, so read permission on it is needed as well.

#### relative path

When the path spelled relative to the cwd is a length the standard library accepts, that is passed. Used only when the path given is absolute. Only the string is rewritten; there is no `chdir`.

- If the path is below the cwd (`getcwd`, or a `$PWD` that names the same directory as the cwd), the prefix is cut off. Otherwise both sides are resolved to real paths and a spelling that climbs with `..` is made. The shorter one is used
- The relative spelling is checked to name the same directory as the path before it is used, and the cwd is checked to be unchanged after the bind / connect ([below](#the-relative-route-is-exposed-to-another-threads-chdir))

#### widened sockaddr

macOS / iOS. XNU takes an address whose `sun_path` runs past 104 bytes, as long as the path is at most 253 bytes. A hand-built sockaddr is passed to libc's `connect(2)` through ctypes. The original path is passed when it is at most 253 bytes, failing that the relative path.

- Only connects use it. On a bind it would leave an address over 104 bytes on the listener, which the standard library's `getsockname` (the implementation that reads the kernel's spelling) reads past (unrelated bytes follow). So a bind does not use widened spellings on the symlink and thread cwd routes either (the hub's DR-0009)

#### symlink

A symlink `sockat.XXXXXXXXXX` (10 random characters) is created in a temporary directory, the bind / connect goes through it, and the symlink is removed right away.

| | the symlink points to | spelling passed | reached through `/tmp` |
|---|---|---|---|
| bind | the socket's directory | `<place>/sockat.XXXXXXXXXX/<file name>` | a file name of up to 84 bytes (80 where `sun_path` is 104) |
| connect | the socket file | `<place>/sockat.XXXXXXXXXX` | whatever the directory depth and the file name length |

- The places tried are `TMPDIR` (when it is a non-empty absolute path), then `/tmp`. The first one where the spelling fits, that is safe, and where the symlink can be created is used
- **A safe place** is a directory that is one of the following. Nothing is created in a place that is not safe; the next one is tried (except for the fd-held form below)
  - sticky and owned by root or by the calling user (as `/tmp` is)
  - owned by the calling user with no write permission for group and others (as macOS's `$TMPDIR` is)
- **The fd-held form** (connects on Linux with `/proc` only): in a place that is not safe, a directory `sockat.XXXXXXXXXX` of mode 0700 is created, opened `O_PATH | O_DIRECTORY | O_NOFOLLOW` and checked to be owned by the calling user with no permission for group and others; inside it, through that fd, a symlink `s` to the socket file is created, and the connect goes to `/proc/self/fd/<fd>/s`. The directory is reached by its fd, not its name, so another user replacing it changes nothing. `s` and the directory are removed afterwards
- The places are tried in the order `TMPDIR` (a symlink if it is safe, the fd-held form if not), then `/tmp` (the same)

#### thread cwd

Linux / Android and macOS / iOS. Only the cwd of a throwaway `threading.Thread` is moved into the directory, and that thread binds / connects by the file name.

| OS | How | When it cannot be used |
|---|---|---|
| Linux / Android | ctypes `unshare(CLONE_FS)` → `os.fchdir` | a seccomp filter is on (docker's default, systemd's `SystemCallFilter=`; `unshare` is then not called, since a filter may kill the process for it), or `unshare` is refused |
| macOS / iOS | ctypes `pthread_fchdir_np` (a private libSystem API) | the function is missing or fails |

- Neither the process's cwd nor any other thread's cwd changes. The peer pid seen by the party connected to is still the caller's
- The file name must fit a sockaddr (107 on Linux, 103 bytes on macOS; 253 for a connect on macOS, with the widened sockaddr)
- How the directory is opened: `O_PATH` on Linux, `O_SEARCH` on macOS (search permission is enough for both)

### How a failing route is treated

When a route actually binds / connects and fails (`ENOENT`, `EADDRINUSE`, `ECONNREFUSED`, ...), that error is returned and the next route is not tried. The next route is tried only when a route cannot be used at all (too long, `/proc` not usable, the kernel refusing the widened sockaddr, the place not being safe or the symlink not being creatable, the thread's cwd not being movable).

### Where a path stops being reached

When a path longer than the standard library accepts is `ENAMETOOLONG` (bytes).

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux (thread cwd usable) | file name > 107 | file name > 107, and the symlink route cannot be used |
| Linux (thread cwd not usable) | the file name does not fit the `/proc` spelling, relative path > 107, and either file name > 84 or the symlink route cannot be used | the file name does not fit the `/proc` spelling, relative path > 107, and the symlink route cannot be used |
| FreeBSD | file name > 103 | file name > 103 |
| macOS (thread cwd usable) | file name > 103 | file name > 253, and the symlink route cannot be used |
| NetBSD / OpenBSD | relative path > 103, and either file name > 80 or the symlink route cannot be used | relative path > 103, and the symlink route cannot be used |
| illumos | relative path > 107, and either file name > 84 or the symlink route cannot be used | relative path > 107, and the symlink route cannot be used |

- On Linux without `/proc`, the condition "does not fit the `/proc` spelling" drops out
- A path longer than `PATH_MAX` as a whole (1024 bytes on macOS, 4096 on Linux) is `ENAMETOOLONG` on every route (no other system call opens it either; it is outside what sockat handles)
- The symlink route cannot be used when there is no safe place where the symlink can be created (a sandbox where neither `TMPDIR` nor `/tmp` can be used, and the like). A connect on Linux with `/proc` still gets through a place that is not safe, through the fd-held form, as long as a directory can be created there

## What differs from the standard library for a long path

### The address is reported as the original path

For a long path, the address the kernel reports (`getsockname(2)` / `getpeername(2)`) is not the original path but the spelling handed to the kernel at bind time. The sockets sockat returns replace it with the original path.

| Socket | Returns the original path | Otherwise |
|---|---|---|
| a listener from `bind` | `getsockname()` | as the standard library |
| a socket returned by the listener's `accept()` | `getsockname()` | `getpeername()` as the standard library (the peer's spelling, usually empty) |
| a socket from `connect` / `connect_nonblocking` | `getpeername()` | `getsockname()` as the standard library (usually empty) |

- The original path is a `str` that went through `os.fsdecode` (the same type the standard library returns for a path address). A path given as a relative path stays relative
- A `dup()`ed socket reports it the same way. A socket rebuilt from an fd with `socket.fromfd` or the like is of the standard type, so it reports the kernel's spelling
- So what reads the address back (asyncio's cleanup, `repr`, logs) works with the original path ([DR-0003](./decisions/DR-0003-report-the-given-path.md), in Japanese)

The spellings the kernel reports are as follows (visible by calling the standard implementation, as in `socket.socket.getsockname(s)`).

| Route of the bind | Reported |
|---|---|
| /proc | `/proc/self/fd/<fd>/<file name>` (the fd is closed by then) |
| relative path | the relative path |
| symlink | a path through a symlink that no longer exists |
| thread cwd, bindat | the file name alone |

The `getpeername()` of a connect to a listener that another program bound with a widened sockaddr (Node.js on macOS, for example) is a truncated path, and on CPython for macOS unrelated bytes may follow the cut (the behaviour of the standard library's `getpeername`; getting it never raises; [cause, in Japanese](./findings/2026-11-18-cpython-getsockname-widened-sockaddr.md)).

### A path that does not end with a name is a ValueError

A path too long for the standard library whose last part is not a file name (`dir/`, `dir/.`, `dir/..`, `..`) gets a `ValueError` before anything is created. A shorter path goes to the standard library unchanged and gets the kernel's error.

### A path containing NUL is a ValueError

A path containing a NUL anywhere but at its start gets a `ValueError` at any length. The standard library silently truncates at the NUL and binds to a different path (`b"/tmp/a\0b"` becomes `/tmp/a`). On Linux / Android a path starting with a NUL is an address in the abstract namespace, so it is handed to the standard library unchanged at any length.

### The directory is looked up slightly before the call

The /proc, relative path, bindat and thread cwd routes look the directory up first (open it, or resolve it to a real path), then bind / connect by the file name. If a directory along the path is renamed, or a symlink along it repointed, in between, the socket is created in the directory as it was when looked up. The widened sockaddr and symlink routes behave as the standard library does.

### The relative route is exposed to another thread's chdir

sockat itself never changes the cwd. If the cwd has changed after the bind / connect, the result is discarded and an `OSError` without an errno is raised (a socket file it may have created is left alone, since where it went is unknown). A change that is undone in between is not visible.

### A symlink can be left behind

If the process dies between creating and removing the symlink (the directory, for the fd-held form), `sockat.XXXXXXXXXX` is left under `TMPDIR` or `/tmp`.

## Errors

| Situation | Error |
|---|---|
| the directory does not exist / nothing to connect to | `FileNotFoundError` (`ENOENT`) |
| a file already exists at the bind path | `OSError` (`EADDRINUSE`) |
| a socket file nobody listens on | `ConnectionRefusedError` (`ECONNREFUSED`) |
| the backlog is full | on Linux `BlockingIOError` for a nonblocking connect (`EAGAIN`; a blocking connect waits), on macOS `ConnectionRefusedError` |
| no route reaches the path ([Where a path stops being reached](#where-a-path-stops-being-reached)) | `OSError` (`ENAMETOOLONG`) |
| a path too long for the standard library that does not end with a name | `ValueError` |
| a path containing a NUL anywhere but at its start | `ValueError` |
| the cwd changed during the relative route | an `OSError` without an errno |

On macOS a connect also returns `ECONNREFUSED` when the peer's backlog is full. Code that removes the socket file on seeing `ECONNREFUSED` can, on macOS, remove the socket file of a live listener (the kernel's behaviour, the same with the standard library).

## asyncio

The results are the standard library's `socket.socket`, so they can be passed straight to an API that takes `sock=`.

```python
server = await asyncio.start_unix_server(handle, sock=sockat.bind(path))

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- It works as with a short path. The asyncio of Python 3.13 and later removes the socket file at the original path when the server is closed (`cleanup_socket`; the listener's `getsockname()` returns the original path, and that is what it removes). The asyncio of 3.11 / 3.12 does not remove it (as with the standard library)
- The transport's `get_extra_info('sockname')` (server side) / `get_extra_info('peername')` (client side) are also the original path

`connect_nonblocking` does not wait for the peer, so it can be called directly from the event loop. `bind` does not wait for a peer either (on the thread cwd route it waits for the thread to end, but the bind returns at once).

## Not checked

- Android and iOS have not been run. They use the same routes as Linux / macOS
- NetBSD, OpenBSD and illumos run the tests on VMs in CI
- The error a nonblocking connect returns for a full backlog was checked only on Linux and macOS
