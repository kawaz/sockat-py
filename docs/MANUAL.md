# sockat manual

> English | [日本語](./MANUAL-ja.md)

A reference for using sockat-py: how it reaches a long path, and how it differs from the standard library for long paths. The language-independent behaviour is defined by the [sockat specification](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md); the design decisions of the Python version are in [DR-0001](./decisions/DR-0001-python-port-design.md), [DR-0004](./decisions/DR-0004-own-directory-route.md) and [DR-0005](./decisions/DR-0005-bind-any.md) (in Japanese).

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
| `sockat.ex.bind(path)` and the like | the same + what was lost, cleaning up the socket file, the last resort | a `Bound` / `Connected` ([below](#sockatex)) |

- For a path the standard library accepts, the result is a plain `socket.socket`. For a long path, it is a subclass of `socket.socket` that reports the address as the original path ([below](#the-address-is-reported-as-the-original-path))
- `path` is a `str` / `bytes` / `os.PathLike` (the standard library's `bind` / `connect` do not take an `os.PathLike`, but sockat does)
- A path that the standard library accepts ([the length per OS](#how-a-long-path-is-reached)) goes to the standard library unchanged
- Whatever the route, the socket is created as `socket.socket(AF_UNIX, SOCK_STREAM)`, so it is set up like the standard library's (close-on-exec, the value of `socket.setdefaulttimeout`, the default listen backlog)
- The socket file is created at the path given. It is not removed when the socket is closed (as with the standard library). To remove it, call `discard()` from `sockat.ex.bind`, or `os.unlink(path)` with the original path (unlink has no `sun_path` limit)
- To change the backlog, call `listen(n)` again on the returned listener

## connect and connect_nonblocking

- **`connect`** blocks. As with the standard library's `connect`, it follows the value of `settimeout`
- **`connect_nonblocking`** is for event loops. The socket is made nonblocking before the connect and returned that way. When the peer's backlog is full it fails at once (`BlockingIOError` on Linux, `ConnectionRefusedError` on macOS). A Unix-domain connect completes or fails within the call, so there is nothing to wait for afterwards

## sockat.ex

The calls with cleaning up and diagnosis ([DR-0006](./decisions/DR-0006-sockat-ex.md), the hub's [DR-0015](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0015-ex-with-last-resort-and-diagnostics.md), in Japanese). `import sockat` does not load it. Whoever uses it chooses it with `import sockat.ex`.

```python
import sockat.ex

listener, discard, losses, route, skipped = sockat.ex.bind(path)
try:
    ...
finally:
    listener.close()
    discard()

stream, losses, route, skipped = sockat.ex.connect(path)
```

| Function | Plain call | Returns |
|---|---|---|
| `sockat.ex.bind(path, *, last_resort=False)` | `sockat.bind(path)` | a `Bound` (`listener`, `discard`, `losses`, `route`, `skipped`) |
| `sockat.ex.connect(path, *, last_resort=False)` | `sockat.connect(path)` | a `Connected` (`stream`, `losses`, `route`, `skipped`) |
| `sockat.ex.connect_nonblocking(path, *, last_resort=False)` | `sockat.connect_nonblocking(path)` | a `Connected` |

- Call them through the module, as `sockat.ex.connect(...)`. Imported as `from sockat.ex import connect`, they are easily mistaken for the plain `sockat.connect`
- Without `last_resort`, each tries the same routes in the same order as its plain call, and reaches what it reaches. The `listener` / `stream` is what the plain call returns
- The results are `NamedTuple`s, read by name or by position

### discard

The function `sockat.ex.bind` returns that removes the socket file the bind made, and nothing else.

- Right after the bind goes through (before the listen), the realpath of the socket file and its `(st_dev, st_ino)` are remembered. If that fails, the listener is closed and the error raised
- `discard()` removes the file at that realpath only while it is still the same file. When it is gone, or replaced by another file, it does nothing
- A replacement between the check and the removal cannot be prevented (POSIX has no way to remove a name only if it still refers to a given inode)
- `discard()` may be called any number of times. It does not close the listener (the caller closes it; once handed to asyncio, asyncio does)
- Errors other than the file being gone (`EACCES` on the directory, ...) are raised as `OSError`
- It works the same for a short path. For a Linux abstract address it does nothing

### last_resort

`last_resort=True` uses the last resort, which reaches the path even by losing something where no route of the plain order can be used. Passing it is the statement that what is lost is accepted.

| Function | The last resort |
|---|---|
| `sockat.ex.bind` | where the plain bind gets `ENAMETOOLONG` (no route can be used), [symlink-dir](#symlink-dir) is tried last. Through a shared place it loses `recreatable-spelling` |
| `sockat.ex.connect`, `sockat.ex.connect_nonblocking` | the specification's last resort is fork-cwd-direct (a forked child connects by the file name). sockat-py has no fork route, so it is put in `skipped` with the reason `no fork route in sockat-py`, and the call gets `ENAMETOOLONG`. It reaches what the plain connect reaches |

### What was lost

| Item | Contents |
|---|---|
| `losses` | what the route taken lost (a `tuple[Loss, ...]`). Empty when nothing was lost. Branch on this |
| `route` | the name of the route taken (a `str`), from [the specification's route names](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#route-names) (`"standard"`, `"symlink-file"`, `"thread-cwd"`, `"symlink-dir"`, ...) |
| `skipped` | the routes tried before `route` that could not be used, in the order tried. Each item is a `Skipped(name, reason)`: `name` is the route's name, `reason` why it could not be used, a short English phrase (`"path too long"`, `"seccomp filter"`, `"/proc unusable"`, `"no place for D"`, ...) |

```python
if sockat.ex.Loss.RECREATABLE_SPELLING in losses:
    ...  # remove the socket file by the original path (discard and the like)
```

`Loss` is an `enum.StrEnum`; as a string (`str()`, `.value`) it is the name in [the specification's "What is lost"](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#what-is-lost).

| `Loss` | Name | What is lost | When |
|---|---|---|---|
| `RESPELLED` | `respelled` | the address the kernel reports is not the original path. The spelling may be seen by other users (Linux's `/proc/net/unix`), so where the process's cwd is a place others can write to, removing a file by the spelling after one of them recreated it may remove a file of the same name in a directory of their choosing. Removing by the original path is safe | a route other than standard was taken |
| `RECREATABLE_SPELLING` | `recreatable-spelling` | the spelling the kernel reports goes through a shared place (`/tmp` and the like). Removing a file by the spelling after another user saw and recreated it may remove a file of the same name in a directory of their choosing. Removing by the original path is safe | symlink-dir went through a shared place |
| `CWD_RELATIVE` | `cwd-relative` | the spelling the kernel keeps is relative to the cwd. Removing or looking up by it after the cwd changed reaches another place (a danger the standard library has of its own). Removing by the original path made absolute is safe | a relative path went through standard (never for an abstract address) |
| `FORKED` | `forked` | the side effects of a fork | never in sockat-py, which has no fork route |

- There is no order of size or inclusion between the names. The names are promised and only ever added to. Read a name you do not know as "something was lost"
- `route` and `skipped` are for diagnosis, and their contents are not promised (routes are added and changed)
- Every route tried before `route` could not be used (a route that actually binds / connects and fails raises there, and nothing after it is tried)
- The specification's fork routes (a bind's `fork-cwd` and `fork-cwd-sockdir`, a connect's last resort `fork-cwd-direct`) appear in `skipped` at their place in the order, with the reason that sockat-py does not have them
- A connect's spelling stays nowhere, so a connect's `losses` are empty (the only loss would be fork-cwd-direct's `forked`, which sockat-py does not have)

| Example of `sockat.ex.bind` (macOS) | `losses` | `route` | `name`s in `skipped` |
|---|---|---|---|
| a short absolute path | (empty) | `standard` | (none) |
| a short relative path | `cwd-relative` | `standard` | (none) |
| a long path | `respelled` | `symlink-file` | `standard` |
| a long path, no place for symlink-file | `respelled` | `thread-cwd` | `standard`, `symlink-file` |
| the same, and D can be created nowhere | `respelled` | `thread-cwd-sockdir` | `standard`, `symlink-file`, `thread-cwd`, `fork-cwd` |
| the same, thread cwd cannot be used either, and symlink-dir has a place only the owner can write to (`XDG_RUNTIME_DIR` and the like) | `respelled` | `symlink-dir` | `standard`, `symlink-file`, `thread-cwd`, `fork-cwd` |
| the same, only a shared place exists | `respelled`, `recreatable-spelling` | `symlink-dir` | `standard`, `symlink-file`, `thread-cwd`, `fork-cwd` |

When a call fails, it raises the same error as its plain counterpart (its type and `errno` unchanged) with two attributes added.

| Attribute | Contents |
|---|---|
| `skipped` | the routes tried and found unusable up to the failure (the same form as `skipped`) |
| `route` | the name of the route whose bind / connect actually failed. `None` when no route got that far (no directory, `ENAMETOOLONG` with no usable route, an argument error) |

```python
try:
    stream, losses, route, skipped = sockat.ex.connect(path)
except OSError as e:
    print(e.errno, getattr(e, "route", None), getattr(e, "skipped", ()))
```

### symlink-dir

The last resort of `sockat.ex.bind(path, last_resort=True)`.

1. Gather the candidate places: `XDG_RUNTIME_DIR`, `TMPDIR` (each when a non-empty absolute path), then `/tmp`. One spelled the same as `/tmp` or an earlier candidate, component by component, is skipped
2. `stat` each candidate, following symlinks, and order those only the owner can write to (a directory, owned by the calling user, `mode & 0o022 == 0`) first, the shared ones (a directory, sticky, owned by root or the calling user) after. A candidate of neither kind is not used
3. In order, check that the spelling `<place>/sockat.XXXXXXXXXX/<file name>` fits `sun_path`, and if it does, create in the place `sockat.XXXXXXXXXX` (10 random characters; a new name on `EEXIST`, giving up after 16) pointing to the realpath of the socket's directory. A candidate where it does not fit or cannot be created is left with nothing in it and the next is tried
4. Bind with that spelling, and remove the symlink on success and on failure alike. When the bind fails (`EADDRINUSE`, ...), that error is raised and no further place is tried
5. When no candidate reaches the path, `ENAMETOOLONG`

- Through a place only the owner can write to, only the calling user can recreate the spelling, so after the symlink is removed the kernel's spelling names nothing
- Through a shared place, after the symlink is removed another user can recreate the same spelling (the spelling is visible in Linux's `/proc/net/unix` and the like). Code that removes a file by the kernel's spelling may then remove a file of the same name in a directory of the other user's choosing. sockat's listener reports the original path, so asyncio's cleanup removes by the original path
- A file name is reached up to `sun_path` − 1 − (the length of the place's spelling + 19) bytes (with `/tmp`, 84 on Linux, 80 on macOS / the BSDs)

## How a long path is reached

A path up to the length the standard library accepts (`sun_path` − 1) is handed to it unchanged (the route standard).

| OS | Handed to the standard library |
|---|---|
| Linux / Android / illumos | up to 107 bytes |
| macOS / iOS / FreeBSD / NetBSD / OpenBSD | up to 103 bytes |

For a longer path, the socket's directory is looked up first (`ENOENT` if it does not exist), then the routes below are tried in this order per OS, and the path is bound / connected on the first route that can be used. None of the routes changes the cwd of the calling process or of the calling thread. Routes are called by [the specification's route names](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#route-names).

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` usable) | [thread-cwd](#the-routes-through-d) → [proc](#the-routes-through-d) | [proc-direct](#routes-only-a-connect-takes) → [thread-cwd](#the-routes-through-d) → [proc](#the-routes-through-d) → [proc-held](#routes-only-a-connect-takes) → [symlink-file](#symlink-file) |
| Linux / Android (`/proc` not usable) | [thread-cwd](#the-routes-through-d) | [relative](#routes-only-a-connect-takes) → [thread-cwd](#the-routes-through-d) → [symlink-file](#symlink-file) |
| FreeBSD | [symlink-file](#symlink-file) → [at](#the-routes-through-d) | [at-direct](#routes-only-a-connect-takes) → [at](#the-routes-through-d) |
| macOS / iOS | [symlink-file](#symlink-file) → [thread-cwd](#the-routes-through-d) | [relative](#routes-only-a-connect-takes) → [widened](#routes-only-a-connect-takes) → [thread-cwd](#the-routes-through-d) → [symlink-file](#symlink-file) |
| NetBSD / OpenBSD / illumos / other Unix | [symlink-file](#symlink-file) | [relative](#routes-only-a-connect-takes) → [symlink-file](#symlink-file) |

- A bind tries [symlink-file](#symlink-file) right after the standard everywhere but Linux / Android. Where a bind follows a symlink (confirmed on macOS), it reaches a path whatever the length of its file name. Where the link is not followed, the next route is tried
- A connect tries the routes that need neither a write (creating D, R or a symlink) nor a thread (proc-direct, relative, widened, at-direct) first, and the routes that create D or start a thread after them. The routes that need neither are fast, and relative and widened reach further than the routes through D in some cases
- D is created at most once per call and shared by the routes that use it (thread-cwd, at, proc, proc-held). When it can be created nowhere, every route that uses it is skipped as unusable, and D is not tried again
- When D can be created nowhere, a bind tries the same means in [the form without D](#the-form-without-d) (thread-cwd-sockdir, at-sockdir, proc-sockdir)
- The specification's fork routes (fork-cwd, fork-cwd-sockdir, fork-cwd-direct) are not built. A child forked from Python runs Python, so it cannot satisfy the specification's requirement that the child's span be async-signal-safe ([DR-0001](./decisions/DR-0001-python-port-design.md)). Where the specification has a bind go on to fork-cwd, the bind gets `ENAMETOOLONG` (ruled by kawaz in the hub's D12-Q3). To reach the path even there, use [the last resort of `sockat.ex.bind`](#last_resort)
- A path that no route in the order reaches gets `ENAMETOOLONG`

### The routes through D

thread-cwd, at and proc.

1. Create a private directory D (mode 0700) in a place, and open and hold it by its fd
2. Check with `fstat` that D is ours (a directory, owned by the calling user, no permission for group and others), and set it to 0700
3. Inside D, create R, a symlink with a 10-character random name pointing to the realpath of the socket's directory
4. Bind / connect to `R/<file name>` relative to D's fd
5. At the end of the call, remove R and D, on success and on failure alike (D is created at most once per call and shared by the routes that use it)

- A path is reached at any directory depth. A file name fits the spelling `R/<file name>` up to 96 bytes on Linux, 92 on Apple's OSes / FreeBSD
- A D that fails the check is taken to be someone else's and is left alone, contents and all
- When the D just created cannot be opened (a umask took off the owner's bits: the search bit on macOS, the read bit on the OSes that open it `O_RDONLY`), it is looked up by name with `stat` (not following a symlink), removed with `rmdir` if it meets the same conditions as step 2, and the place counts as one where D cannot be created, so the next is tried. Even if it is swapped between the check by name and the removal, `rmdir` removes only an empty directory (the hub's D12-Q5)
- The length and whether a means can be used are checked before D is created. If not, nothing is created and the next step is tried
- The length of proc's spelling depends on the number of D's fd, so it is estimated before D is created and checked again once D is open. When it does not fit, proc cannot be used and the next route is tried. D is shared with the later routes and removed once, at the end of the call

#### Where D is created

| Order | Place | Name of D |
|---|---|---|
| 1 | `${TMPDIR:-/tmp}` (a relative path is fine) | `sockat.XXXXXXXXXX` |
| 2 | `/tmp` (only when 1 fails; skipped when the same as 1) | `sockat.XXXXXXXXXX` |
| 3 | the socket's directory (only when 2 fails too) | `.sockat.XXXXXXXXXX` |

- Whether the place is safe (sticky and the like) and how long it is do not matter. D is held by its fd, and the place does not appear in the spelling
- D is opened `O_PATH` on Linux, `O_SEARCH` on macOS (both open even when a umask took the owner's read bit off), `O_RDONLY` elsewhere
- When D can be created nowhere, a bind goes on to [the form without D](#the-form-without-d), and a connect skips the routes that use D and goes on to the next route in its order

#### Means

| Route | OS | How | When it cannot be used |
|---|---|---|---|
| thread-cwd | Linux / Android | in a throwaway `threading.Thread`, ctypes `unshare(CLONE_FS)` → `os.fchdir(D)` → `R/<file name>` | a seccomp filter is on (docker's default, systemd's `SystemCallFilter=`, ...; `unshare` is then not called, since a filter may kill the process for it), `unshare` is refused, or no thread can be started |
| thread-cwd | macOS / iOS | in a throwaway `threading.Thread`, ctypes `pthread_fchdir_np(D)` (a private libSystem API) → `R/<file name>` | the function is missing or fails, or no thread can be started |
| at | FreeBSD | ctypes `bindat(2)` / `connectat(2)` with D's fd and `R/<file name>` | file name > 92 |
| proc | Linux / Android | `/proc/self/fd/<fd of D>/R/<file name>` passed to the standard library | `/proc/self/fd/<fd>` cannot be looked up (a chroot or a minimal container with no `/proc`), or the spelling is over 107 bytes (a file name of up to about 79) |

- With thread-cwd, neither the process's cwd nor any other thread's cwd changes. The peer pid seen by the party connected to is still the caller's
- On Linux, when thread-cwd cannot be used, proc is tried with the same D

#### The form without D

Only a bind tries it, when D can be created nowhere. The routes are thread-cwd-sockdir (at-sockdir on FreeBSD), then proc-sockdir.

1. Inside the socket's directory, create R, a symlink pointing to the realpath of the socket's directory, relative to the fd of the socket's directory. Its name is `.sockat.` and 10 random characters
2. Relative to the fd of the socket's directory, bind to `.sockat.XXXXXXXXXX/<file name>` with the [means](#means) in the same order. thread-cwd-sockdir moves a throwaway thread's cwd into the socket's directory; proc-sockdir passes `/proc/self/fd/<fd of the socket's directory>/.sockat.XXXXXXXXXX/<file name>`
3. Remove R, on success and on failure alike

- The spelling is 19 bytes plus the file name. A file name fits up to 88 bytes on Linux, 84 on macOS / FreeBSD (about 72 with proc-sockdir)
- When the socket's directory cannot be opened, no means can be used, or the spelling does not fit, nothing is created and the bind gets `ENAMETOOLONG`. So does a bind for which R cannot be created (16 name collisions in a row, and the like)
- When D could be created but no means could be used, this form is not tried and the bind gets `ENAMETOOLONG`

### Routes only a connect takes

Only connects use these routes. They are tried in the order per OS (the table in [how a long path is reached](#how-a-long-path-is-reached)), side by side with the routes through D.

| Route | How | When it can be used |
|---|---|---|
| proc-direct | `/proc/self/fd/<fd>/<file name>` with the fd of the socket's directory, passed to the standard library | Linux / Android. The socket's directory can be opened, and the spelling is at most 107 bytes (a file name of up to about 90) |
| proc-held | in the D shared with the routes through D (created if there is none yet), create a symlink `s` to the socket file, connect to `/proc/self/fd/<fd of D>/s`, then remove them | Linux / Android. D can be created somewhere and `s` in it ([DR-0002](./decisions/DR-0002-unsafe-base-held-by-fd.md), in Japanese) |
| relative | pass the path relative to the cwd to the standard library. Only the string is rewritten; there is no `chdir` | the path given is absolute, and the relative path is a length the standard library accepts |
| widened | pass the original path (failing that, the relative path) to libc's `connect(2)` through ctypes in a sockaddr that carries up to 253 bytes past `sun_path` | macOS / iOS. The path is at most 253 bytes, and the kernel accepts it |
| at-direct | ctypes `connectat(2)` with the fd of the socket's directory and the file name | FreeBSD. The socket's directory can be opened, and the file name is at most 103 bytes |

- relative: if the path is below the cwd (`getcwd`, or a `$PWD` that names the same directory as the cwd), the prefix is cut off. Otherwise both sides are resolved to real paths and a spelling that climbs with `..` is made. The shorter one is used
- relative: the spelling is checked to name the same directory as the path before it is used, and the cwd is checked to be unchanged after the connect ([below](#relative-is-exposed-to-another-threads-chdir))
- `bind` does not use proc-direct, relative, widened or symlink-dir. Their spelling later hits an unrelated file (proc-direct through a reused fd number, relative through a changed cwd), or the listener's address is cut short and the standard library's `getsockname` reads past it (widened), or the routes through D are enough (symlink-dir) ([DR-0004](./decisions/DR-0004-own-directory-route.md), in Japanese). Only `sockat.ex.bind` with the last resort uses symlink-dir, last ([above](#symlink-dir))

### symlink-file

Binds (everywhere but Linux / Android) and connects use it.

1. Look at the places `TMPDIR` (when it is an absolute path other than `/tmp`), then `/tmp`, and in the first where the spelling `<place>/sockat.XXXXXXXXXX` fits `sun_path` (for a connect on macOS, up to 253 bytes with the widened sockaddr) and which is a safe place, create `sockat.XXXXXXXXXX` (10 random characters; on `EEXIST` another, up to 16 tries) pointing to the path of the socket file (with the cwd in front for a relative path). Nothing is created in a place where it does not fit, that is not safe, or where it cannot be created; the next one is tried
2. Bind / connect with that spelling, and remove the symlink on success and on failure alike

- **A safe place** is a directory that is one of the following
  - sticky and owned by root or by the calling user (as `/tmp` is)
  - owned by the calling user with no write permission for group and others (as macOS's `$TMPDIR` is)
- Neither the file name nor the directory goes into the spelling, so a path is reached whatever the length of its file name
- A bind goes through it only on an OS whose bind follows a symlink in the last component (confirmed on macOS; Linux / Android do not follow it, so it is not tried there; other OSes are not confirmed). When the bind fails, the original path is looked up with `lstat`: if something is there, that error (`EADDRINUSE` and the like) is returned and nothing more is tried; if nothing is there, or it cannot be told, the kernel is taken not to have followed the link, so the route cannot be used and the next route is tried (the hub's [DR-0016](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0016-bind-through-file-symlink.md), in Japanese)
- A bind through it loses only `respelled`. The address the kernel reports is the symlink's spelling; unlinking that spelling removes at most an entry in the place (the symlink is already gone), so it cannot be made to remove an unrelated file

### How a failing route is treated

When a route actually binds / connects and fails (`ENOENT`, `EADDRINUSE`, `ECONNREFUSED`, ...), that error is returned and neither the next route nor the next place is tried. The next one is tried only when a route cannot be used at all (the spelling does not fit, D or R cannot be created, the means cannot be used, `/proc` is not usable, the kernel refuses the widened sockaddr, the symlink's place is not safe or the symlink cannot be created, a bind through symlink-file failed with nothing at the original path).

### Where a path stops being reached

When a path longer than the standard library accepts is `ENAMETOOLONG` (given that the directory exists and D can be created; bytes).

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` usable) | file name > 96 (about 79 when thread-cwd cannot be used) | reached (proc-held) |
| Linux / Android (`/proc` not usable) | file name > 96, or thread-cwd cannot be used | relative > 107, thread-cwd cannot be used (file name > 96 or thread-cwd unusable), and symlink-file cannot be used |
| FreeBSD | file name > 92 (symlink-file not counted) | file name > 103 |
| macOS / iOS | macOS: reached (symlink-file). When symlink-file cannot be used, file name > 92, or thread-cwd cannot be used<br>other Apple OSes: file name > 92, or thread-cwd cannot be used | both the original and the relative path > 253, thread-cwd cannot be used (file name > 92 or thread-cwd unusable), and symlink-file cannot be used |
| NetBSD / OpenBSD | every long path (symlink-file not counted) | relative > 103, and symlink-file cannot be used |
| illumos | every long path (symlink-file not counted) | relative > 107, and symlink-file cannot be used |

- When D can be created nowhere, a bind reaches a file name of up to 88 bytes (84 on macOS / FreeBSD, about 72 with proc-sockdir alone) through [the form without D](#the-form-without-d) (when the socket's directory can be opened and R created in it; where no means can be used, nothing is reached, as in the table above). A connect gets as far as the routes in its order that do not use D reach (proc-direct, relative, widened, at-direct, symlink-file; proc-held cannot be used either)
- In the bind column, symlink-file is counted on macOS only. Linux / Android do not try it, their bind not following a symlink. Whether the other OSes follow it is not confirmed
- `sockat.ex.bind` with the last resort reaches the path even where `bind` gets `ENAMETOOLONG`, when the spelling of [symlink-dir](#symlink-dir), `<place>/sockat.XXXXXXXXXX/<file name>`, fits `sun_path` and a usable place exists
- symlink-file and symlink-dir cannot be used when there is no safe place where the symlink can be created (a sandbox where neither `TMPDIR` nor `/tmp` can be used, and the like)
- A path longer than `PATH_MAX` as a whole (1024 bytes on macOS, 4096 on Linux) is `ENAMETOOLONG` on every route (no other system call opens it either; it is outside what sockat handles)

## What differs from the standard library for a long path

### The address is reported as the original path

For a long path, the address the kernel reports (`getsockname(2)` / `getpeername(2)`) is not the original path but the spelling handed to the kernel at bind time. The sockets sockat returns replace it with the original path.

| Socket | Returns the original path | Otherwise |
|---|---|---|
| a listener from `bind` / `sockat.ex.bind` | `getsockname()` | as the standard library |
| a socket returned by the listener's `accept()` | `getsockname()` | `getpeername()` as the standard library (the peer's spelling, usually empty) |
| a socket from `connect` / `connect_nonblocking` | `getpeername()` | `getsockname()` as the standard library (usually empty) |

- The original path is a `str` that went through `os.fsdecode` (the same type the standard library returns for a path address). A path given as a relative path stays relative
- A `dup()`ed socket reports it the same way. A socket rebuilt from an fd with `socket.fromfd` or the like is of the standard type, so it reports the kernel's spelling
- So what reads the address back (asyncio's cleanup, `repr`, logs) works with the original path ([DR-0003](./decisions/DR-0003-report-the-given-path.md), in Japanese)

The spellings the kernel reports are as follows (visible by calling the standard implementation, as in `socket.socket.getsockname(s)`). R and the symlink are gone after the bind, so the spelling resolved later names nothing as a rule. The spelling may be seen by other users, though (Linux's `/proc/net/unix`), so when the process's cwd is a place others can write to, and through symlink-dir in a shared place, other users can recreate it ([`losses` of `sockat.ex`](#what-was-lost)). Remove the socket file by the original path, not by the spelling. R has a random name to avoid hitting something of the same name that happens to be there, not as a defence against other users.

| Route of the bind | Reported |
|---|---|
| thread-cwd, at | `R/<file name>` |
| proc | `/proc/self/fd/<fd>/R/<file name>` |
| thread-cwd-sockdir, at-sockdir | `.sockat.XXXXXXXXXX/<file name>` |
| proc-sockdir | `/proc/self/fd/<fd>/.sockat.XXXXXXXXXX/<file name>` |
| symlink-file | `<place>/sockat.XXXXXXXXXX`. Unlinking that spelling removes at most an entry in the place, never an unrelated file |
| symlink-dir (the last resort of `sockat.ex.bind`) | `<place>/sockat.XXXXXXXXXX/<file name>`. In a shared place, other users can recreate it |

The `getpeername()` of a connect to a listener that another program bound with a widened sockaddr (Node.js on macOS, for example) is a truncated path, and on CPython for macOS unrelated bytes may follow the cut (the behaviour of the standard library's `getpeername`; getting it never raises; [cause, in Japanese](./findings/2026-11-18-cpython-getsockname-widened-sockaddr.md)).

### A path that does not end with a name is a ValueError

A path too long for the standard library whose last part is not a file name (`dir/`, `dir/.`, `dir/..`, `..`) gets a `ValueError` before anything is created. A shorter path goes to the standard library unchanged and gets the kernel's error.

### A path containing NUL is a ValueError

A path containing a NUL anywhere but at its start gets a `ValueError` at any length. The standard library silently truncates at the NUL and binds to a different path (`b"/tmp/a\0b"` becomes `/tmp/a`). On Linux / Android a path starting with a NUL is an address in the abstract namespace, so it is handed to the standard library unchanged at any length.

### The directory is looked up slightly before the call

The routes through D, the form without D and symlink-dir resolve the socket's directory to a real path when they create R or the symlink, and the connect routes proc-direct, relative and at-direct look the directory up first (open it, or resolve it to a real path), then bind / connect by the file name. If a directory along the path is renamed, or a symlink along it repointed, in between, the socket is created in the directory as it was when looked up. widened, symlink-file and proc-held behave as the standard library does.

### relative is exposed to another thread's chdir

sockat itself never changes the cwd. If the cwd has changed after the connect, the result is discarded and an `OSError` without an errno is raised. A change that is undone in between is not visible.

### The form without D in a socket's directory others can write to

In a socket's directory that is not sticky and that others can write to, R of the form without D can be replaced between its creation and the bind, so the bind may go to another directory (a name already there gives `EADDRINUSE`; nothing is overwritten). R inside D cannot be replaced, D being 0700.

### Leftovers can remain

If the process dies between creating and removing them, the following are left.

| Where | What is left |
|---|---|
| the place (`TMPDIR` or `/tmp`; for symlink-dir also `XDG_RUNTIME_DIR`) | `sockat.XXXXXXXXXX` (D, or the symlink of symlink-file or of symlink-dir) |
| the socket's directory | `.sockat.XXXXXXXXXX` (D, or R of the form without D) |

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
| the cwd changed during relative | an `OSError` without an errno |

On macOS a connect also returns `ECONNREFUSED` when the peer's backlog is full. Code that removes the socket file on seeing `ECONNREFUSED` can, on macOS, remove the socket file of a live listener (the kernel's behaviour, the same with the standard library).

## asyncio

The results are the standard library's `socket.socket`, so they can be passed straight to an API that takes `sock=`.

```python
server = await asyncio.start_unix_server(handle, sock=sockat.bind(path))

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- It works as with a short path, with no extra arguments. The asyncio of Python 3.13 and later removes the socket file at the original path when the server is closed (`cleanup_socket`; the listener's `getsockname()` returns the original path, and that is what it removes). The asyncio of 3.11 / 3.12 does not remove it (as with the standard library)
- The transport's `get_extra_info('sockname')` (server side) / `get_extra_info('peername')` (client side) are also the original path
- The `listener` / `stream` from `sockat.ex` can be passed the same way. `discard()` may be called while the server runs or after it has closed

`connect_nonblocking` does not wait for the peer, so it can be called directly from the event loop. `bind` does not wait for a peer either (with thread-cwd it waits for the thread to end, but the bind returns at once).

## Not checked

- Android and iOS have not been run. They use the same routes as Linux / macOS
- FreeBSD, NetBSD, OpenBSD and illumos run the tests on VMs in CI
- The error a nonblocking connect returns for a full backlog was checked only on Linux and macOS
- No test takes the last resort symlink-dir on FreeBSD (at cannot be made unusable, so a bind that goes on to symlink-dir cannot be set up)
- Whether the bind of FreeBSD, NetBSD, OpenBSD and illumos follows symlink-file's link is not confirmed (the tests on the CI VMs tell)
