# sockat

> English | [日本語](./README-ja.md)

A Python library that binds and connects Unix domain sockets even at paths past the `sun_path` limit (104 bytes including the NUL on macOS and the BSDs, 108 bytes on Linux).

The language-independent behaviour is defined by [the sockat specification](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md); sockat-py is its Python implementation.

## Usage

```sh
pip install sockat
```

Just rewrite the standard code with sockat. What is returned is the standard `socket.socket`, and a short path is passed to the standard library as it is, so the behaviour does not change.

```python
import sockat

# listener = socket.socket(socket.AF_UNIX); listener.bind(path); listener.listen()
listener = sockat.bind(path)

# client = socket.socket(socket.AF_UNIX); client.connect(path)
client = sockat.connect(path)
```

| Function | Standard code it replaces | Returns |
|---|---|---|
| `sockat.bind(path)` | `socket()` → `bind(path)` → `listen()` | a listening `socket.socket` |
| `sockat.connect(path)` | `socket()` → `connect(path)` | `socket.socket` |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | a non-blocking `socket.socket` (for event loops such as asyncio) |
| `sockat.datagram.bind(path)` / `unbound()` / `connect(sock, path)` | the same code with `SOCK_DGRAM` | `socket.socket` / `None` |
| `sockat.ex.bind(path, options=...)` and so on | the same + spelling properties, clean up, diagnostics | `Bound` / `Connected` / `Report` ([MANUAL](./docs/MANUAL.md#sockatex)) |
| `sockat.ex.resolve_for_bind` / `resolve_for_connect` | a short spelling you pass to the standard library yourself | `ForBind` / `ForConnect` ([MANUAL](./docs/MANUAL.md#resolve)) |
| `sockat.ex.sweep()` / `sweep_base(path)` | — | `None` ([MANUAL](./docs/MANUAL.md#sweep)) |

- `path` is `str` / `bytes` / `os.PathLike`
- Python 3.11 or later. No dependencies (pure Python)

## Things to know

- Binding to a long path leaves small directories and symlinks in `/tmp/sockat-<uid>/` and the like. What remains and what removes it are in the specification's "[What stays in the base](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#what-stays-in-the-base)" and "[`sweep`](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#sweep)". In environments that run tests over and over, calling `sockat.ex.sweep()` once cleans them up
- What differs from the standard for a long path is in the specification's "[What differs from the standard for a long path](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#what-differs-from-the-standard-for-a-long-path)", and the patterns to avoid in "[Patterns not to use](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md#patterns-not-to-use)" (their form in Python and the code to write instead are in the [MANUAL](./docs/MANUAL.md#patterns-not-to-use))
- Notes specific to Python (the subclass that returns the original path as the address, asyncio and so on) are in [the MANUAL's "Notes specific to Python"](./docs/MANUAL.md#notes-specific-to-python)

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

## Documentation

- [docs/MANUAL.md](./docs/MANUAL.md) — names and types in Python, the Python form of the patterns not to use, notes specific to Python, error types
- [The sockat specification](https://github.com/kawaz/sockat/blob/main/docs/SPEC.md) — the language-independent behaviour ([building the routes](https://github.com/kawaz/sockat/blob/main/docs/SPEC-routes.md), [per-language caveats](https://github.com/kawaz/sockat/blob/main/pages/langs.md))
- [docs/decisions/](./docs/decisions/INDEX.md) — design decisions of the Python version (DRs)

## License

MIT License, Yoshiaki Kawazu (@kawaz)
