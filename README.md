# sockat

> English | [日本語](./README-ja.md)

A Unix domain socket path is too long to bind / connect. sockat is a Python library that makes that go away.

Give a long path to Python's standard `socket`, and both bind and connect refuse it with this error. asyncio's `start_unix_server(path=...)` and `open_unix_connection(path)` do the same.

```text
OSError: AF_UNIX path too long
```

Replace the standard code with sockat, and long paths just work.

```python
import sockat

# listener = socket.socket(socket.AF_UNIX); listener.bind(path); listener.listen()
listener = sockat.bind(path)

# client = socket.socket(socket.AF_UNIX); client.connect(path)
client = sockat.connect(path)

# With asyncio, pass the socket made to sock=
server = await asyncio.start_unix_server(handle, sock=sockat.bind(path))
reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

You get back the same `socket.socket` as the standard, and short paths go to the standard unchanged, as before. Only for a long path does sockat quietly set up a short way to the same place and connect through it. You don't need to shorten paths yourself or care about each OS's limit.

## Install

```sh
pip install sockat
```

It runs on Python 3.11 or later and depends on no other package.

## More

- [User manual](./docs/MANUAL.md)
- [Reference](./docs/REFERENCE.md)
- [How it works](https://github.com/kawaz/sockat/blob/main/pages/technical.md)

## License

MIT License, Yoshiaki Kawazu (@kawaz)
