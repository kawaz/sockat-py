# sockat manual

> English | [日本語](./MANUAL-ja.md)

A reference for using sockat-py: how it reaches a long path, and how it differs from the standard library for long paths. The language-independent behaviour is defined by the [sockat specification](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md); the design decisions of the Python version are in [DR-0001](./decisions/DR-0001-python-port-design.md) and [DR-0004](./decisions/DR-0004-own-directory-route.md) (in Japanese).

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
| `sockat.bind_ex(path)` | the same + cleaning up the socket file | `(listener, discard)` ([below](#bind_ex)) |
| `sockat.connect(path)` | `socket()` → `connect(path)` | a `socket.socket` (blocking) |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | a nonblocking `socket.socket` |

- For a path the standard library accepts, the result is a plain `socket.socket`. For a long path, it is a subclass of `socket.socket` that reports the address as the original path ([below](#the-address-is-reported-as-the-original-path))
- `path` is a `str` / `bytes` / `os.PathLike` (the standard library's `bind` / `connect` do not take an `os.PathLike`, but sockat does)
- A path that the standard library accepts ([the length per OS](#how-a-long-path-is-reached)) goes to the standard library unchanged
- Whatever the route, the socket is created as `socket.socket(AF_UNIX, SOCK_STREAM)`, so it is set up like the standard library's (close-on-exec, the value of `socket.setdefaulttimeout`, the default listen backlog)
- The socket file is created at the path given. It is not removed when the socket is closed (as with the standard library). To remove it, call `discard()` from `bind_ex`, or `os.unlink(path)` with the original path (unlink has no `sun_path` limit)
- To change the backlog, call `listen(n)` again on the returned listener

## connect and connect_nonblocking

- **`connect`** blocks. As with the standard library's `connect`, it follows the value of `settimeout`
- **`connect_nonblocking`** is for event loops. The socket is made nonblocking before the connect and returned that way. When the peer's backlog is full it fails at once (`BlockingIOError` on Linux, `ConnectionRefusedError` on macOS). A Unix-domain connect completes or fails within the call, so there is nothing to wait for afterwards

## bind_ex

```python
listener, discard = sockat.bind_ex(path)
try:
    ...
finally:
    listener.close()
    discard()
```

- `listener` is the same as from `sockat.bind(path)`
- Right after the bind goes through (before the listen), the realpath of the socket file and its `(st_dev, st_ino)` are remembered. If that fails, the listener is closed and the error raised
- `discard()` removes the file at that realpath only while it is still the same file. When it is gone, or replaced by another file, it does nothing
- A replacement between the check and the removal cannot be prevented (POSIX has no way to remove a name only if it still refers to a given inode)
- `discard()` may be called any number of times. It does not close the listener (the caller closes it; once handed to asyncio, asyncio does)
- Errors other than the file being gone (`EACCES` on the directory, ...) are raised as `OSError`
- It works the same for a short path. For a Linux abstract address it does nothing
- The name is provisional ([DR-0004](./decisions/DR-0004-own-directory-route.md), in Japanese)

## How a long path is reached

A path up to the length the standard library accepts (`sun_path` − 1) is handed to it unchanged.

| OS | Handed to the standard library |
|---|---|
| Linux / Android / illumos | up to 107 bytes |
| macOS / iOS / FreeBSD / NetBSD / OpenBSD | up to 103 bytes |

For a longer path, the socket's directory is looked up first (`ENOENT` if it does not exist), then the routes below are tried in this order per OS, and the path is bound / connected on the first route that can be used. None of the routes changes the cwd of the calling process or of the calling thread.

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` usable) | [thread cwd](#means) → [/proc variant](#means) | [thread cwd](#means) → [/proc variant](#means) → [/proc](#fallback-routes) → [fd-held form](#fallback-routes) → [symlink](#fallback-routes) |
| Linux / Android (`/proc` not usable) | [thread cwd](#means) | [thread cwd](#means) → [relative path](#fallback-routes) → [symlink](#fallback-routes) |
| FreeBSD | [bindat](#means) | [connectat](#means) → [connectat (socket's directory)](#fallback-routes) |
| macOS / iOS | [thread cwd](#means) | [thread cwd](#means) → [relative path](#fallback-routes) → [widened sockaddr](#fallback-routes) → [symlink](#fallback-routes) |
| NetBSD / OpenBSD / illumos / other Unix | none (`ENAMETOOLONG`) | [relative path](#fallback-routes) → [symlink](#fallback-routes) |

- Thread cwd, the /proc variant and bindat / connectat are the means of [the central route](#the-central-route). When D can be created nowhere, a bind tries the same means in [the form without D](#the-form-without-d)
- The specification's fork route is not built. A child forked from Python runs Python, so it cannot satisfy the specification's requirement that the child's span be async-signal-safe ([DR-0001](./decisions/DR-0001-python-port-design.md)). Where the specification has a bind go on to fork, the bind gets `ENAMETOOLONG` (ruled by kawaz in the hub's D12-Q3)
- A path that no route in the order reaches gets `ENAMETOOLONG`

### The central route

1. Create a private directory D (mode 0700) in a place, and open and hold it by its fd
2. Check with `fstat` that D is ours (a directory, owned by the calling user, no permission for group and others), and set it to 0700
3. Inside D, create R, a symlink with a 10-character random name pointing to the realpath of the socket's directory
4. Bind / connect to `R/<file name>` relative to D's fd
5. Remove R and D, on success and on failure alike

- A path is reached at any directory depth. A file name fits the spelling `R/<file name>` up to 96 bytes on Linux, 92 on Apple's OSes / FreeBSD
- A D that fails the check is taken to be someone else's and is left alone, contents and all
- The length and whether a means can be used are checked before D is created. If not, nothing is created and the next step is tried

#### Where D is created

| Order | Place | Name of D |
|---|---|---|
| 1 | `${TMPDIR:-/tmp}` (a relative path is fine) | `sockat.XXXXXXXXXX` |
| 2 | `/tmp` (only when 1 fails; skipped when the same as 1) | `sockat.XXXXXXXXXX` |
| 3 | the socket's directory (only when 2 fails too) | `.sockat.XXXXXXXXXX` |

- Whether the place is safe (sticky and the like) and how long it is do not matter. D is held by its fd, and the place does not appear in the spelling
- D is opened `O_PATH` on Linux, `O_SEARCH` on macOS (both open even when a umask took the owner's read bit off), `O_RDONLY` elsewhere
- When D can be created nowhere, a bind goes on to [the form without D](#the-form-without-d), a connect to [the fallback routes](#fallback-routes)

#### Means

| Means | OS | How | When it cannot be used |
|---|---|---|---|
| thread cwd | Linux / Android | in a throwaway `threading.Thread`, ctypes `unshare(CLONE_FS)` → `os.fchdir(D)` → `R/<file name>` | a seccomp filter is on (docker's default, systemd's `SystemCallFilter=`, ...; `unshare` is then not called, since a filter may kill the process for it), `unshare` is refused, or no thread can be started |
| thread cwd | macOS / iOS | in a throwaway `threading.Thread`, ctypes `pthread_fchdir_np(D)` (a private libSystem API) → `R/<file name>` | the function is missing or fails, or no thread can be started |
| bindat / connectat | FreeBSD | ctypes `bindat(2)` / `connectat(2)` with D's fd and `R/<file name>` | file name > 92 |
| /proc variant | Linux / Android | `/proc/self/fd/<fd of D>/R/<file name>` passed to the standard library | `/proc/self/fd/<fd>` cannot be looked up (a chroot or a minimal container with no `/proc`), or the spelling is over 107 bytes (a file name of up to about 79) |

- With thread cwd, neither the process's cwd nor any other thread's cwd changes. The peer pid seen by the party connected to is still the caller's
- On Linux, when thread cwd cannot be used, the /proc variant is tried with the same D

#### The form without D

Only a bind tries it, when D can be created nowhere.

1. Inside the socket's directory, create R, a symlink pointing to the realpath of the socket's directory, relative to the fd of the socket's directory. Its name is `.sockat.` and 10 random characters
2. Relative to the fd of the socket's directory, bind to `.sockat.XXXXXXXXXX/<file name>` with the [means](#means) in the same order (thread cwd, bindat on FreeBSD → the /proc variant). Thread cwd moves a throwaway thread's cwd into the socket's directory; the /proc variant passes `/proc/self/fd/<fd of the socket's directory>/.sockat.XXXXXXXXXX/<file name>`
3. Remove R, on success and on failure alike

- The spelling is 19 bytes plus the file name. A file name fits up to 88 bytes on Linux, 84 on macOS / FreeBSD (about 72 with the /proc variant)
- When the socket's directory cannot be opened, no means can be used, or the spelling does not fit, nothing is created and the bind gets `ENAMETOOLONG`. So does a bind for which R cannot be created (16 name collisions in a row, and the like)
- When D could be created but no means could be used, this form is not tried and the bind gets `ENAMETOOLONG`

### Fallback routes

Only connects try these, in the order per OS, when the central route cannot be used (no means, D cannot be created, the file name does not fit).

| Route | How | When it can be used |
|---|---|---|
| /proc | `/proc/self/fd/<fd>/<file name>` with the fd of the socket's directory, passed to the standard library | Linux / Android. The socket's directory can be opened, and the spelling is at most 107 bytes (a file name of up to about 90) |
| fd-held form | create the same D as the central route, create a symlink `s` to the socket file inside it, connect to `/proc/self/fd/<fd of D>/s`, then remove them | Linux / Android. D can be created somewhere ([DR-0002](./decisions/DR-0002-unsafe-base-held-by-fd.md), in Japanese) |
| relative path | pass the path relative to the cwd to the standard library. Only the string is rewritten; there is no `chdir` | the path given is absolute, and the relative path is a length the standard library accepts |
| widened sockaddr | pass the original path (failing that, the relative path) to libc's `connect(2)` through ctypes in a sockaddr that carries up to 253 bytes past `sun_path` | macOS / iOS. The path is at most 253 bytes, and the kernel accepts it |
| symlink | create `sockat.XXXXXXXXXX` pointing to the socket file in a place, pass it, and remove it at once | a safe place exists and the spelling fits (on macOS up to 253 bytes with the widened sockaddr) |
| connectat (socket's directory) | ctypes `connectat(2)` with the fd of the socket's directory and the file name | FreeBSD. The socket's directory can be opened, and the file name is at most 103 bytes |

- Relative path: if the path is below the cwd (`getcwd`, or a `$PWD` that names the same directory as the cwd), the prefix is cut off. Otherwise both sides are resolved to real paths and a spelling that climbs with `..` is made. The shorter one is used
- Relative path: the spelling is checked to name the same directory as the path before it is used, and the cwd is checked to be unchanged after the connect ([below](#the-relative-route-is-exposed-to-another-threads-chdir))
- The places tried for the symlink are `TMPDIR` (when it is an absolute path other than `/tmp`), then `/tmp`
- **A safe place** for the symlink is a directory that is one of the following. Nothing is created in a place that is not; the next one is tried
  - sticky and owned by root or by the calling user (as `/tmp` is)
  - owned by the calling user with no write permission for group and others (as macOS's `$TMPDIR` is)
- A bind does not use /proc (`/proc/self/fd/<fd>/<file name>`, not through R), the relative path, the widened sockaddr or a symlink in a place. Their spelling later hits an unrelated file (/proc through a reused fd number, the relative path through a changed cwd), or the listener's address is cut short and the standard library's `getsockname` reads past it (the widened sockaddr), or the central route is enough (the symlink) ([DR-0004](./decisions/DR-0004-own-directory-route.md), in Japanese)

### How a failing route is treated

When a route actually binds / connects and fails (`ENOENT`, `EADDRINUSE`, `ECONNREFUSED`, ...), that error is returned and neither the next route nor the next place is tried. The next one is tried only when a route cannot be used at all (the spelling does not fit, D or R cannot be created, the means cannot be used, `/proc` is not usable, the kernel refuses the widened sockaddr, the symlink's place is not safe or the symlink cannot be created).

### Where a path stops being reached

When a path longer than the standard library accepts is `ENAMETOOLONG` (given that the directory exists and D can be created; bytes).

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` usable) | file name > 96 (about 79 when thread cwd cannot be used) | reached (fd-held form) |
| Linux / Android (`/proc` not usable) | file name > 96, or thread cwd cannot be used | the central route cannot be used (file name > 96 or thread cwd unusable), relative path > 107, and the symlink route cannot be used |
| FreeBSD | file name > 92 | file name > 103 |
| macOS / iOS | file name > 92, or thread cwd cannot be used | the central route cannot be used (file name > 92 or thread cwd unusable), both the original and the relative path > 253, and the symlink route cannot be used |
| NetBSD / OpenBSD | every long path | relative path > 103, and the symlink route cannot be used |
| illumos | every long path | relative path > 107, and the symlink route cannot be used |

- When D can be created nowhere, a bind reaches a file name of up to 88 bytes (84 on macOS / FreeBSD, about 72 with the /proc variant alone) through [the form without D](#the-form-without-d) (when the socket's directory can be opened and R created in it; where no means can be used, nothing is reached, as in the table above). A connect gets as far as the fallback routes reach (the fd-held form cannot be used)
- The symlink route cannot be used when there is no safe place where the symlink can be created (a sandbox where neither `TMPDIR` nor `/tmp` can be used, and the like)
- A path longer than `PATH_MAX` as a whole (1024 bytes on macOS, 4096 on Linux) is `ENAMETOOLONG` on every route (no other system call opens it either; it is outside what sockat handles)

## What differs from the standard library for a long path

### The address is reported as the original path

For a long path, the address the kernel reports (`getsockname(2)` / `getpeername(2)`) is not the original path but the spelling handed to the kernel at bind time. The sockets sockat returns replace it with the original path.

| Socket | Returns the original path | Otherwise |
|---|---|---|
| a listener from `bind` / `bind_ex` | `getsockname()` | as the standard library |
| a socket returned by the listener's `accept()` | `getsockname()` | `getpeername()` as the standard library (the peer's spelling, usually empty) |
| a socket from `connect` / `connect_nonblocking` | `getpeername()` | `getsockname()` as the standard library (usually empty) |

- The original path is a `str` that went through `os.fsdecode` (the same type the standard library returns for a path address). A path given as a relative path stays relative
- A `dup()`ed socket reports it the same way. A socket rebuilt from an fd with `socket.fromfd` or the like is of the standard type, so it reports the kernel's spelling
- So what reads the address back (asyncio's cleanup, `repr`, logs) works with the original path ([DR-0003](./decisions/DR-0003-report-the-given-path.md), in Japanese)

The spellings the kernel reports are as follows (visible by calling the standard implementation, as in `socket.socket.getsockname(s)`). R is gone after the bind, so wherever the spelling is resolved later, it names nothing.

| Means of the bind | Reported |
|---|---|
| thread cwd, bindat | `R/<file name>` |
| /proc variant | `/proc/self/fd/<fd>/R/<file name>` |
| thread cwd, bindat in the form without D | `.sockat.XXXXXXXXXX/<file name>` |
| /proc variant in the form without D | `/proc/self/fd/<fd>/.sockat.XXXXXXXXXX/<file name>` |

The `getpeername()` of a connect to a listener that another program bound with a widened sockaddr (Node.js on macOS, for example) is a truncated path, and on CPython for macOS unrelated bytes may follow the cut (the behaviour of the standard library's `getpeername`; getting it never raises; [cause, in Japanese](./findings/2026-11-18-cpython-getsockname-widened-sockaddr.md)).

### A path that does not end with a name is a ValueError

A path too long for the standard library whose last part is not a file name (`dir/`, `dir/.`, `dir/..`, `..`) gets a `ValueError` before anything is created. A shorter path goes to the standard library unchanged and gets the kernel's error.

### A path containing NUL is a ValueError

A path containing a NUL anywhere but at its start gets a `ValueError` at any length. The standard library silently truncates at the NUL and binds to a different path (`b"/tmp/a\0b"` becomes `/tmp/a`). On Linux / Android a path starting with a NUL is an address in the abstract namespace, so it is handed to the standard library unchanged at any length.

### The directory is looked up slightly before the call

The central route and the form without D resolve the socket's directory to a real path when they create R, and the /proc, relative path and connectat fallback routes look the directory up first (open it, or resolve it to a real path), then bind / connect by the file name. If a directory along the path is renamed, or a symlink along it repointed, in between, the socket is created in the directory as it was when looked up. The widened sockaddr, symlink and fd-held routes behave as the standard library does.

### The relative route is exposed to another thread's chdir

sockat itself never changes the cwd. If the cwd has changed after the connect, the result is discarded and an `OSError` without an errno is raised. A change that is undone in between is not visible.

### Leftovers can remain

If the process dies between creating and removing them, the following are left.

| Where | What is left |
|---|---|
| the place (`TMPDIR` or `/tmp`) | `sockat.XXXXXXXXXX` (D, or a connect's symlink) |
| the socket's directory | `.sockat.XXXXXXXXXX` (D, or R of the form without D) |

- On macOS, when a umask takes the owner's search bit off (0777 / 0177 and the like, for a user other than root), the D just created cannot be opened and cannot be told to be ours, so it is left in place. On the OSes that open it `O_RDONLY`, the same happens with the read bit

## Errors

| Situation | Error |
|---|---|
| the directory does not exist / nothing to connect to | `FileNotFoundError` (`ENOENT`) |
| the directory part of a long path is not a directory | `NotADirectoryError` (`ENOTDIR`) |
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

- It works as with a short path, with no extra arguments. The asyncio of Python 3.13 and later removes the socket file at the original path when the server is closed (`cleanup_socket`; the listener's `getsockname()` returns the original path, and that is what it removes). The asyncio of 3.11 / 3.12 does not remove it (as with the standard library)
- The transport's `get_extra_info('sockname')` (server side) / `get_extra_info('peername')` (client side) are also the original path
- The listener from `bind_ex` can be passed the same way. `discard()` may be called while the server runs or after it has closed

`connect_nonblocking` does not wait for the peer, so it can be called directly from the event loop. `bind` does not wait for a peer either (with thread cwd it waits for the thread to end, but the bind returns at once).

## Not checked

- Android and iOS have not been run. They use the same routes as Linux / macOS
- FreeBSD, NetBSD, OpenBSD and illumos run the tests on VMs in CI
- The error a nonblocking connect returns for a full backlog was checked only on Linux and macOS
