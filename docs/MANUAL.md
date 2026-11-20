# sockat manual

> English | [日本語](./MANUAL-ja.md)

A reference for using sockat-py, how it reaches long paths, and what differs from the standard library for a long path. The language-independent behaviour is defined by [the sockat specification](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md) (how routes are built in [SPEC-routes](https://github.com/kawaz/sockat/blob/main/docs/SPEC-routes.md), how far they reach in [the reach table](https://github.com/kawaz/sockat/blob/main/pages/reach/README.md)), and the design decisions of the Python version are in [DR-0001](./decisions/DR-0001-python-port-design.md).

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

| Function | Standard code it replaces | Returns |
|---|---|---|
| `sockat.bind(path)` | `socket()` → `bind(path)` → `listen()` | a listening `socket.socket` |
| `sockat.connect(path)` | `socket()` → `connect(path)` | `socket.socket` |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | a non-blocking `socket.socket` |
| `sockat.datagram.bind(path)` | `socket(AF_UNIX, SOCK_DGRAM)` → `bind(path)` | `socket.socket` |
| `sockat.datagram.unbound()` | `socket(AF_UNIX, SOCK_DGRAM)` | `socket.socket` |
| `sockat.datagram.connect(sock, path)` | `sock.connect(path)` | `None` |
| `sockat.ex.bind(path, options=options)` and so on | the same + spelling properties, clean up, diagnostics | `Bound` / `Connected` / `Report` ([sockat.ex](#sockatex)) |
| `sockat.ex.resolve_for_bind(path, options=options)` / `resolve_for_connect` | a short spelling you pass to the standard library yourself | `ForBind` / `ForConnect` ([resolve](#resolve)) |
| `sockat.ex.sweep()` / `sweep_base(path)` | — | `None` ([sweep](#sweep)) |

- For a path of a length the standard library accepts, what is returned is a plain `socket.socket`. For a long path, it is a subclass of `socket.socket` that returns the original path as its address ([Addresses return the original path](#addresses-return-the-original-path))
- `path` is `str` / `bytes` / `os.PathLike`
- A path of a length the standard library accepts ([the value per OS](#how-long-paths-are-reached)) is handed to the standard library as it is, with nothing added (not even environment variables are read)
- The socket of every route is made with `socket.socket(AF_UNIX, SOCK_STREAM)` (`SOCK_DGRAM` for DGRAM), so it gets the same settings as the standard library (close-on-exec, the value of `socket.setdefaulttimeout`, the default listen backlog)
- The socket file is made at the path you gave. It is not removed when the socket is closed (the same as the standard library). To remove it, use `file.remove()` of `sockat.ex.bind`, or `os.unlink(path)` with the original path (unlink has no `sun_path` limit)
- To change the backlog, call `listen(n)` again on the returned listener

## connect and connect_nonblocking

- **`connect`** blocks. Like the standard `connect`, it follows the value of `settimeout`
- **`connect_nonblocking`** is for event loops. It makes the socket non-blocking before the connect and returns it as it is. If the peer's backlog is full it fails without waiting (`BlockingIOError` on Linux, `ConnectionRefusedError` on macOS). A Unix domain connect either completes or fails within the call, so there is nothing to wait for afterwards
- Neither leaves anything behind (even when it goes as far as link-file, the E it made is removed right after the connect)

## sockat.datagram

Of the standard DGRAM calls, it mirrors only the functions that take a path, and `unbound`, which is used together with them ([the specification's "Functions"](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#functions)).

```python
import sockat

server = sockat.datagram.bind(path)
client = sockat.datagram.unbound()
sockat.datagram.connect(client, path)
client.send(b"hello")
```

- `bind` tries the same routes as the stream bind (it does not listen). While the spelling is alive, replies to a sender bound to a long path (`sendto` to the address `recvfrom` gave) reach it
- `connect` tries the same routes as the stream connect, with the socket you pass. It does not close the socket on failure (closing it is up to the user)
- `send_to` (sending to a path) and SEQPACKET are not there yet (open in the specification)

## sockat.ex

It is not loaded by `import sockat`. The user opts in with `import sockat.ex`. Call it through the module, as in `sockat.ex.connect(...)` (importing it as `from sockat.ex import connect` mixes it up with the default `sockat.connect` and the standard `socket.connect_ex`).

```python
import asyncio

import sockat.ex

bound = sockat.ex.bind(path)
server = await asyncio.start_unix_server(handle, sock=bound.socket)
...
bound.file.remove()  # before closing the listener
server.close()

connected = sockat.ex.connect(path)
```

| Function | Returns |
|---|---|
| `sockat.ex.bind(path, *, options=None)` | `Bound(socket, file, spelling, route, base, skipped)` |
| `sockat.ex.connect(path, *, options=None)` / `connect_nonblocking` | `Connected(socket, route, skipped)` |
| `sockat.ex.datagram.bind(path, *, options=None)` | `Bound` (the socket is DGRAM) |
| `sockat.ex.datagram.connect(sock, path, *, options=None)` | `Report(route, skipped)` |
| `sockat.ex.resolve_for_bind(path, *, options=None)` | `ForBind(path, spelling, route, base, skipped)` and `cleanup()`, `socket_file()` |
| `sockat.ex.resolve_for_connect(path, *, options=None)` | `ForConnect(path, route, skipped)` and `cleanup()` |
| `sockat.ex.sweep()` / `sockat.ex.sweep_base(path)` | `None` |

- They try the same routes in the same order as the default calls
- `options` can only be passed by keyword (`sockat.ex.bind(path, options=sockat.ex.Options(...))`). The fields of `Options` are given by keyword too
- `Bound` / `Connected` / `Report` are named tuples and can be unpacked in the order above

### Options

| Field | Meaning |
|---|---|
| `usable_cross_uid: bool = False` | Asks for a spelling a peer of another uid can also follow. Only the bind calls (`bind`, `datagram.bind`, `resolve_for_bind`) look at it; the connect calls do not (a connect's spelling is not left with the peer). B is taken from a place where every directory on the way and B itself grant x to other (`/tmp`), and procB (`/proc/<pid>/fd/...`) is not used. If this cannot be met, it does not fall back to a spelling that does not meet it, but goes on to thread-cwd / at (`Dangling`) (whose D is made in the usual B). Whether it was met is told by `spelling` of the result |
| `base: str \| bytes \| os.PathLike \| None = None` | Gives the path of B itself (a location the app manages itself, e.g. `/tmp/app-<uid>/sockat`). Both the bind calls and the connect calls use it as the place of B. An absolute path ending in a name (otherwise `ValueError`). For a long path, it is checked without making anything after the socket's directory is looked up and before routes are tried (the directories on the way, and B itself if present. If the check fails, `route` is `None` and `skipped` is empty). B is made when a route that uses B is reached (its parent must exist). If the check fails it is an error and does not fall back to the automatic places ([Errors](#errors)) |

### Spelling properties (Spelling)

Whether the spelling of the bind (what the kernel remembers and `getsockname` returns) can be used later is returned in `Bound.spelling` / `ForBind.spelling`. Branch on it.

| Value | Spelling | Route |
|---|---|---|
| `Original()` | the original absolute path | standard |
| `CwdRelative()` | the original relative path. It points elsewhere once the cwd changes (a danger the standard library already has, but it is reported) | standard |
| `Linked(removable, until, usable_cross_uid, usable_with_private_tmp)` | goes through B, D and E and points to the same place as the original path | link-dir, link-file |
| `Dangling()` | contains an E already cleaned up. Reaches nothing | thread-cwd, at |

| Attribute of `Linked` | Meaning |
|---|---|
| `removable` | Whether unlinking by the spelling removes the real file. `True` for link-dir, `False` for link-file (the last symlink is not followed and E is removed) |
| `until` | `Until.PROCESS` (procB: unfollowable once the process ends) or `Until.CLEANUP` (B's real path: E stays until `cleanup()`) |
| `usable_cross_uid` | Whether a peer of another uid can follow it. `False` for procB. For B's real path, `True` if every directory on the way and B itself grant x to other |
| `usable_with_private_tmp` | Whether it can be followed even if you or the peer isolate `/tmp` (systemd's `PrivateTmp` and so on). Always `True` for procB. For B's real path, `False` if either the place's path before realpath or B's real path goes through `/tmp` or `/var/tmp` (or their realpaths, such as macOS's `/private/tmp`) |

```python
match bound.spelling:
    case sockat.ex.Linked(removable=True):
        ...  # unlinking by the spelling removes the socket file
    case sockat.ex.Original() | sockat.ex.CwdRelative():
        ...  # the path itself
    case _:
        ...  # it does not: remove by the path or file.remove()
```

- Values may be added. Read an unknown value as "the spelling cannot be used"

### remove and cleanup

The two handles of `Bound.file` (`SocketFile`). Each works only when called (neither closing the listener nor garbage collection removes anything). `remove()` tries again on every call until it has succeeded, and does nothing after that. `cleanup()` works once (`remove()` goes on to `cleanup()` even when it fails).

| Handle | What it does |
|---|---|
| `file.remove()` | Removes the socket file that was bound, and also does `cleanup()` |
| `file.cleanup()` | Removes the E the spelling goes through (link-dir, link-file). The socket file remains. Do not use that spelling afterwards |

- `remove()` only removes the file and does not close the listener. Closing it is up to the user
- Right after the bind succeeds (before the listener is returned), it records the realpath and `(st_dev, st_ino)` of the socket file, and removes the path only when its `(st_dev, st_ino)` is the same. If they cannot be recorded, it closes the listener and returns an error without removing the file
- For a relative path, it compares the cwd's `(st_dev, st_ino)` at the start of the call and after recording, and if they differ it closes the listener with an `OSError` without errno (so as not to record an unrelated file in the original cwd because of a cwd change in between. A change and change back is not seen)
- It also succeeds when the file is already gone and when it has been replaced by another file and is not removed. It fails when it cannot check (`lstat` fails with something other than `ENOENT` / `ENOTDIR`) and when the removal itself fails
- Replacement is told apart only by `(st_dev, st_ino)`, and what is promised covers replacement while the listener is open. Something removed and made again after closing cannot be told apart on file systems that reuse numbers at once (ext4, the BSDs' UFS / FFS). **Call `remove()` before closing the listener**
- Swapping the file, between the check and the removal, by someone who can write to the socket's directory cannot be prevented (POSIX has no way to remove by inode)
- The default `sockat.bind` does not return `file`, so its E remains until the process ends and goes away together with D when the OS cleans its temporary directories

### Diagnostics and errors

| Field | Meaning |
|---|---|
| `route` | The name of the route taken ([Route names](#route-names)) |
| `base` | The B the spelling of the bind goes through. For a B in a temporary place, the form of the spelling (`"proc"` for procB; `"tmp"`, `"runtime"`, `"tmpdir"` for the real path); for a B not in a temporary place, that B regardless of the form of the spelling (`"sockdir"` for the socket's directory, `"given"` for `Options.base`). `None` for a spelling that does not go through B (standard, thread-cwd, at). Not in `Connected`, `Report` and `ForConnect` |
| `skipped` | The routes tried that could not be used (in the order tried). Each element is `Skipped(name, reason)`. One entry per route name |

- All the routes tried before `route` could not be used (if a route actually binds / connects and fails, the error is returned there and the next one is not tried)
- `route`, `base`, `skipped` and `reason` are for diagnostics and their contents are not promised (routes may be added or changed). Branch on `spelling`
- On failure, the exception (whose type and `errno` are the same as the default calls') carries `skipped` and `route` attributes. `route` is the name of the route that actually bound / connected and failed, and `None` when no route got that far (`ENAMETOOLONG` because none could be used, and so on)

### resolve

Rewrites a long path into a short spelling that keeps pointing to the same place as the original path, and returns it. The spelling is just a path, so you can pass it to the standard library yourself, whatever the socket type and operation.

```python
import socket

import sockat.ex

r = sockat.ex.resolve_for_connect(path)
for _ in range(3):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(r.path)
        ...
r.cleanup()
```

| | `resolve_for_bind` | `resolve_for_connect` |
|---|---|---|
| Spelling | link-dir (`B/D/E/<file name>`), then link-file (`B/D/E`) on OSes where bind was measured to follow a symlink (macOS, NetBSD) | link-file (`B/D/E`). Regardless of the file name's length |
| Returns | `ForBind` (`path`, `spelling`, `cleanup()`, `socket_file()`) | `ForConnect` (`path`, `cleanup()`. It has no handle to remove) |
| When not usable | `ENAMETOOLONG` (a length only thread-cwd / at reach) | `ENAMETOOLONG` (B cannot be made, the spelling does not fit) |

- A path no longer than what is passed to the standard library is returned as it is (`str` / `bytes` in the type given)
- To reach also where it gives `ENAMETOOLONG`, use `bind` / `connect`
- When connecting to the same path repeatedly, make the spelling once with `resolve_for_connect`, use it many times, and `cleanup()` at the end
- `ForBind` has no `remove()` (at resolve time nothing is bound yet, so the file's `(st_dev, st_ino)` cannot be recorded). Calling `socket_file()` after binding yourself and before closing the listener returns a `SocketFile` that has recorded the socket file by following the spelling. Its `remove()` works the same as `Bound.file`'s, and its `cleanup()` cleans up the same E as `ForBind`'s. Calling it before the bind is an error (`FileNotFoundError` when there is no file, an `OSError` with `ENOTSOCK` when what the spelling leads to is not a socket: an unrelated file at the path is never recorded and removed), and replacement between the bind and the call cannot be told apart. For a relative spelling (`CwdRelative`), if the cwd differs between the resolve and the call, it is an `OSError` without errno

```python
import socket

import sockat.ex

r = sockat.ex.resolve_for_bind(path)
listener = socket.socket(socket.AF_UNIX)
listener.bind(r.path)
listener.listen()
file = r.socket_file()
...
file.remove()
listener.close()
```

### sweep

`sockat.ex.sweep()` removes, from the B with the default names in automatic places 1 to 3 (`/tmp/sockat-<uid>`, `$XDG_RUNTIME_DIR/sockat`, `$TMPDIR/sockat-<uid>`), the D whose owning process has ended (a D whose lock is released). `sockat.ex.sweep_base(path)` does the same for a B passed to `Options.base` (called by the app that manages that B. `path` is checked the same as `Options.base`, and nothing is made).

- It does not remove the D of other live processes. It works only when called (sockat never calls it on its own)
- It does not make B. It does not touch an E directly under B (the default connect's), and leaves a D that contains something other than a symlink
- A B with a random name fallen back to after a failed check (`sockat-<uid>.XXXXXXXXXX`) and a B in the socket's directory (`.sockat.XXXXXXXXXX`) are not covered. These two may not be removed by the OS's clean up either (a B in the socket's directory remains in the user's directory)

## How long paths are reached

A path of a length the standard library accepts (`sun_path` − 1) is passed to it as it is (route standard).

| OS | Length passed to the standard library as it is |
|---|---|
| Linux / Android / illumos | up to 107 bytes |
| macOS / iOS / FreeBSD / NetBSD / OpenBSD | up to 103 bytes |

For a longer path, the routes are tried in the following order, and the first route that can be used binds / connects.

| OS | bind | connect / connect_nonblocking |
|---|---|---|
| Linux / Android (`/proc` usable) | link-dir (procB → B's real path) → thread-cwd | proc-direct → thread-cwd-direct → link-file |
| Linux / Android (`/proc` not usable) | link-dir (B's real path) → thread-cwd | relative → thread-cwd-direct → link-file |
| macOS / iOS | link-dir (B's real path) → link-file → thread-cwd | relative → widened → thread-cwd-direct → link-file |
| FreeBSD | link-dir (B's real path) → at | at-direct → link-file |
| NetBSD | link-dir (B's real path) → link-file | relative → link-file |
| OpenBSD / illumos | link-dir (B's real path) | relative → link-file |
| Other Unix | link-dir (B's real path) → link-file (whether it is followed is told at run time) | relative → link-file |
| Windows | not supported (`ImportError` on import) | same |

- A route the OS has no means for (thread-cwd on NetBSD, OpenBSD and illumos, thread-cwd-direct on an OS where a thread cannot move its cwd (other than Linux / Android and Apple's OSes)) is not in that OS's row, so it does not go into `skipped`. Where the OS has the means but it cannot be used (a seccomp filter and so on), it goes into `skipped` with a reason
- No route changes the cwd of the calling process or thread, so it can be used from multithreaded programs. It does not fork
- A path that fits in no route fails with `ENAMETOOLONG` (an `OSError` with errno), the same as a path too long for the standard library

### The places B, D, E

The spelling of a bind of a long path goes through directories and symlinks that are kept rather than removed.

| | B | D | E |
|---|---|---|---|
| What it is | a real directory in the place (0711) | a directory in B (0711, random name) | a symlink |
| Unit it is made for | per user. Reused across processes. A process uses one B of the automatic places (it may use one more only with `usable_cross_uid`) | one per (process, B). Made only by a bind that keeps its spelling and by `resolve_for_connect` | per resolve. Per call for thread-cwd / at and for the link-file of the default connect |
| While alive | not removed | the process keeps an fd holding a shared flock lock on it | the E of a resolve stays until `cleanup()` |
| Clean up | sockat does not | sockat does not. The D of a dead process goes away when the OS cleans its temporary directories. Only when `sweep()` is called, a D whose lock is released is removed | the E of a resolve by `cleanup()`. A per-call E is removed within the call |

B's place is the first of the following where it can be made (only that one when `Options.base` is given).

| Order | Place | Name of B | `base` in diagnostics (when spelled by the real path) |
|---|---|---|---|
| 1 | `/tmp` | `sockat-<uid>` | `tmp` |
| 2 | `$XDG_RUNTIME_DIR` (when an absolute path) | `sockat` | `runtime` |
| 3 | `$TMPDIR` (when an absolute path) | `sockat-<uid>` | `tmpdir` |
| 4 | the socket's directory | `.sockat.XXXXXXXXXX` | `sockdir` (usable only with procB) |
| — | `Options.base` | the given name | `given` |

- The place is used after being turned into its realpath (macOS's `/tmp` is `/private/tmp`). It is not used unless every directory from the root to the place is owned by root or yourself and not writable by others (including the group), or is sticky (the same idea as OpenSSH's StrictModes). `$TMPDIR` and `$XDG_RUNTIME_DIR` are not used if empty or relative
- B is made with `mkdirat`, set to 0711 by its name right away (giving back what the umask took), opened with `O_NOFOLLOW` and checked to be a real directory owned by yourself. If it already exists it is checked the same way (including that others cannot write to it), and used without changing its mode even if it is not 0711. If the check fails, a B named `sockat-<uid>.XXXXXXXXXX` is made beside it. If the mode cannot be set, it cannot be opened, or the check fails, the empty directory made is removed and the next place is tried. D is made by the same steps; if it cannot be made, that call does without the routes that use D (no other B is looked for, and a later call tries again)
- When the length of the place's real path shows that the spelling will not fit, B is not made
- A process uses one B among places 1 to 3 (even when the default connect made it first, that B is used from then on). A B in the socket's directory is per directory
- D is made with a random name, and the process keeps an fd holding a shared flock lock on it. After taking the lock the name is looked up again, and if another process's `sweep()` removed it, it is made again. A forked child does not use the parent's B and D but makes its own D
- B is spelled first as procB (`/proc/<pid>/fd/<B's fd>`, only when what is looked up through `/proc` has the same `(st_dev, st_ino)` as B's fd), and otherwise as B's real path

### Route names

A name is "means + what it is about". The suffix `-direct` is the form that passes the file name without going through E.

| Route | Operation | How |
|---|---|---|
| standard | bind, connect | Passes to the standard library as it is |
| link-dir | bind | Makes in D an E pointing to the realpath of the socket's directory, and binds with `B/D/E/<file name>`. E stays until `cleanup()` |
| link-file | bind, connect | Makes an E pointing to the socket file. Bind and resolve use an E in D as `B/D/E`; the default connect connects to a random E directly under B as `B/E` with B's real path and removes it at once. The file name does not appear in the spelling, so it does not depend on the file name's length. Bind tries it only on OSes that follow a symlink in the last component (confirmed on macOS and NetBSD), and not on OSes confirmed not to follow it (Linux / Android, FreeBSD, OpenBSD, illumos). On other OSes, if the bind fails and nothing is at the original path, it is taken as not followed and the next route is tried |
| thread-cwd | bind | Makes in D a random E pointing to the socket's directory, moves the cwd of a throwaway thread only to D, binds with `E/<file name>`, and removes E at once (`unshare(CLONE_FS)` → `fchdir` on Linux / Android, `pthread_fchdir_np` on macOS / iOS) |
| at | bind | Passes the same spelling as thread-cwd to `bindat(2)` with D's fd (FreeBSD) |
| proc-direct | connect | Passes `/proc/self/fd/<fd of the socket's directory>/<file name>` (Linux / Android) |
| relative | connect | Passes a path relative to the cwd. Only the string is rewritten; the cwd is not changed |
| widened | connect | Passes a sockaddr that holds up to 253 bytes, which the macOS kernel accepts |
| at-direct | connect | Passes the fd of the socket's directory and the file name to `connectat(2)` (FreeBSD) |
| thread-cwd-direct | connect | Moves the cwd of a throwaway thread only to the socket's directory and passes the file name (Linux / Android, macOS / iOS) |

- Bind does not use proc-direct, relative, widened or at-direct. The spelling would later hit an unrelated file (proc-direct, relative, at-direct), or the address would be cut off midway and break the reader (widened)
- The thread cwd is not used on Linux when a seccomp filter is in place (docker's default, systemd's `SystemCallFilter=` and so on) (some filters kill the process when `unshare` is called)
- The caller waits until the throwaway thread ends. Even if the wait is interrupted (`KeyboardInterrupt`), it cleans up after the thread ends and re-raises the interrupt

### How a failing route is treated

If a route actually binds / connects and fails (`ENOENT`, `EADDRINUSE`, `ECONNREFUSED` and so on), that error is returned and the next route is not tried. It moves on only when the route itself cannot be used (the spelling does not fit, B / D / E cannot be made, there is no means, `/proc` cannot be used, the kernel does not accept widened, the kernel did not follow the link-file bind, the directory of the relative spelling does not match the original or the cwd has already changed, there is no B that meets `usable_cross_uid`).

### How far it reaches

The length of the spelling decides which file names are reached (figures are bytes; the sockaddr is 107 on Linux and 103 elsewhere).

| Route | File names reached |
|---|---|
| link-dir (procB) | 107 − (`/proc/<pid>/fd/<fd>/` + D 10 + E + separators). About 70 |
| link-dir (B's real path) | sockaddr − (B's real path + D 10 + E + separators). About 77 with `/tmp/sockat-1000`, about 66 with macOS's `/private/tmp/sockat-501` |
| link-file | regardless of the file name (as long as the spelling `B/D/E` fits) |
| thread-cwd, at | sockaddr − 11 (96 on Linux, 92 elsewhere) |
| proc-direct | 107 − `/proc/self/fd/<fd>/` (about 90) |
| relative | if the whole relative path fits in the sockaddr |
| widened | the whole path (or relative path) up to 253 |
| at-direct, thread-cwd-direct | sockaddr |

The full picture of how far it reaches under which conditions is in [the reach table](https://github.com/kawaz/sockat/blob/main/pages/reach/README.md).

## What differs from the standard for a long path

### Addresses return the original path

- `getsockname()` of a listener (and of an accepted socket) and `getpeername()` of a connected socket return the original path. The kernel returns the spelling of the bind (`/proc/<pid>/fd/<fd>/D/E/<file name>`, `<B>/D/E/<file name>`, `<B>/D/E`, `E/<file name>`), but the sockets sockat returns replace it with the original path (a subclass of `socket.socket`)
- Where a standard socket fails (`EBADF` after closing, `ENOTCONN` for `getpeername` of a listener), it fails the same way
- The address shown by operational tools such as `ss -x`, `lsof` and `/proc/net/unix` is the spelling of the bind. For link-dir and thread-cwd / at the file name remains at the end of the spelling. The spelling of link-file does not show the file name
- The sender's address `recvfrom` returns for DGRAM is the kernel's spelling. While the spelling is alive, `sendto` to it reaches the sender
- On illumos, `getpeername` on the connecting side (the kernel's value) returns the spelling passed to connect, not the spelling that was bound

### State grows outside the app's own place

- B, D and E remain in `/tmp`, `$XDG_RUNTIME_DIR` and `$TMPDIR`. The E of the default `bind` and the D of a dead process remain until the OS cleans up (10 days for Linux's `/tmp`, until reboot for macOS's `/tmp`, until logout for `$XDG_RUNTIME_DIR`)
- There are environments where a live D may be removed: `/tmp` on Linux older than systemd v242 (RHEL 8 and so on), and when B is in macOS's `$TMPDIR`. The spelling only becomes unfollowable; there is no danger. A B or D removed is made again by the next call (it is let go when its fd's `st_nlink` is 0 or its name no longer names what the fd holds)
- Across exec, the fd holding D's flock is closed, and even if an inherited listener is alive, D and E may be removed by `sweep()` or by the OS's clean up. A procB spelling becomes unfollowable at the exec
- Do not use a spelling after it is cleaned up. When B is in `/tmp`, someone else may make it again with the same name after B is gone
- A B in the socket's directory (`.sockat.XXXXXXXXXX`) and a B fallen back to after a failed check (`sockat-<uid>.XXXXXXXXXX`) may remain, removed neither by `sweep()` nor by the OS's clean up (a B in the socket's directory remains in the user's directory)

### A peer with PrivateTmp

If the peer's `/tmp` is isolated (systemd's `PrivateTmp` and so on), the peer cannot follow a spelling through `/tmp`. On Linux procB is used first, so this rarely happens. Whether it can be followed is told by `Linked.usable_with_private_tmp`.

### When the listener is closed

Python's standard library does not remove the socket file when the listener is closed. sockat does not remove it either (for a listener of `sockat.ex.bind`, even for a short path, only `file.remove()` removes it).

### The directory is looked up first

The E of link-dir and thread-cwd / at points to the realpath of the socket's directory, and proc-direct, at-direct and thread-cwd-direct look up the directory first and then pass the file name. A rename or a symlink re-pointing in between is not reflected.

### relative is weak against chdir in other threads

If the cwd stays changed during relative, the result is discarded and an `OSError` without errno is returned (it never reaches somewhere else). A change and change back is not seen.

### Paths not ending in a name, NUL

- A path of a length that cannot be passed to the standard library as it is and that does not end in a name (ending in `dir/`, `dir/.` or `..`) raises `ValueError` without making anything
- A path that contains a NUL other than at the start raises `ValueError` regardless of its length (the standard library silently truncates at the NUL). On Linux a path starting with a NUL (abstract namespace) is passed to the standard library as it is. On OSes without an abstract namespace a leading NUL is also `ValueError`

## Patterns not to use

Patterns of using sockat that remove unrelated files or fail to remove files ([the specification's "Patterns not to use"](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#patterns-not-to-use)). The code to write instead is read out of this document and run by the test (`tests/test_docs.py`). `path` is the socket's path.

### Removing by the spelling the kernel returns without looking at Spelling

- Pattern: unlinking / stat-ing the socket file by the spelling the kernel returns, as in `os.unlink(socket.socket.getsockname(listener))`
- Why not: with a link-file spelling, only E is removed and the file remains. A `Dangling` spelling reaches nothing
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
- Why not: after clean up, a B in `/tmp` may be made again with the same name by someone else. A procB spelling is unfollowable once the process ends
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

### Binding by a relative path and removing by that relative path after changing the cwd

- Pattern: after `sockat.bind("server.sock")`, calling `os.chdir(...)` and removing with `os.unlink("server.sock")`
- Why not: it hits a file somewhere else (`CwdRelative`)
- Instead: make the path absolute before the bind. `file.remove()`

```python
import os

import sockat.ex

path = os.path.abspath(path)
bound = sockat.ex.bind(path)
os.chdir("/")
...
bound.file.remove()
bound.socket.close()
```

### Counting on closing the listener to remove the socket file

- Pattern: finishing clean up with `listener.close()` alone
- Why not: Python's standard library does not remove it on close. The listener of `sockat.ex.bind` does not remove it on close either
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
- Why not: `remove()` only removes the file
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
- Why not: another file removed and made again after the close may not be told apart ([remove and cleanup](#remove-and-cleanup))
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
- Why not: it cannot follow it (procB only for the same uid, a spelling under a 0700 place only for yourself)
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
- Why not: they are for diagnostics and their contents are not promised (routes may be added or changed)
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

| Situation | Error |
|---|---|
| The directory does not exist / nothing to connect to | `FileNotFoundError` (`ENOENT`) |
| The directory part of a long path is not a directory | `NotADirectoryError` (`ENOTDIR`) |
| A file already exists where to bind | `OSError` (`EADDRINUSE`) |
| A socket file nobody listens on | `ConnectionRefusedError` (`ECONNREFUSED`) |
| The backlog is full | On Linux, `BlockingIOError` (`EAGAIN`) for a non-blocking connect (a blocking connect waits); on macOS, `ConnectionRefusedError` |
| No route can be used (the spelling does not fit, there is no means) | `OSError` (`ENAMETOOLONG`) |
| A length only thread-cwd / at reach for `resolve_for_bind`, link-file not usable for `resolve_for_connect` | `OSError` (`ENAMETOOLONG`) |
| `Options.base` is a relative path, or does not end in a name (`dir/`, `..`, `/`) | `ValueError` |
| The parent of `Options.base` does not exist | `FileNotFoundError` (`ENOENT`) |
| `Options.base` (or its parent) is not a directory | `NotADirectoryError` (`ENOTDIR`) |
| `Options.base` fails the owner / permission check (including, for the bind calls together with `usable_cross_uid`, whether every directory on the way and base itself grant x to other) | `PermissionError` (`EPERM`) |
| A path of a length that cannot be passed to the standard library as it is does not end in a name | `ValueError` |
| A path contains a NUL other than at the start | `ValueError` |
| The cwd changed during the relative route, or during `sockat.ex.bind` of a relative path | `OSError` without errno |

- An error with `Options.base` does not fall back to the automatic places. For a long path it is checked before routes are tried, so `route` is `None` and `skipped` is empty
- On macOS, connect returns `ECONNREFUSED` also when the peer's backlog is full. Code that removes the socket file on seeing `ECONNREFUSED` may, on macOS, remove the socket file of a live listener (this is kernel behaviour and the same with the standard library)

## asyncio

What is returned is the standard `socket.socket`, so it can be passed as it is to APIs that take `sock=`.

```python
server = await asyncio.start_unix_server(handle, sock=sockat.bind(path))

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- Works the same as for a short path without extra arguments. From Python 3.13 on, asyncio removes the socket file at the original path when the server is closed (`cleanup_socket`. The listener's `getsockname()` returns the original path, so that is what it removes). asyncio of 3.11 / 3.12 does not remove it (the same as the standard library)
- The transport's `get_extra_info('sockname')` (server side) / `get_extra_info('peername')` (client side) are also the original path
- The `socket` of `sockat.ex` can be passed the same way. Call `file.remove()` before closing the server (while it is running)

`connect_nonblocking` does not wait for the peer, so it can be called directly from the event loop. `bind` does not wait for a peer either (with thread-cwd it waits for the thread to end, but that ends right after the bind).

## What is not confirmed

- Android and iOS have not been run. They use the same routes as Linux / macOS
- FreeBSD, NetBSD, OpenBSD and illumos run the tests on CI VMs
- The error a non-blocking connect returns when the backlog is full has been checked only on Linux and macOS
- Whether link-file can be used for connect on FreeBSD (unconfirmed in the specification)
