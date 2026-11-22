# Resolving just the path (resolve)

> English | [日本語](./resolve-ja.md)

Keep your own way of binding / connecting (the standard library's `socket`, another library), and make only the path a spelling that fits the limit.

## Connect

The spelling is not needed after the connect, so `cleanup()` right away, whatever the outcome.

```python
import socket

import sockat.ex

client = socket.socket(socket.AF_UNIX)
r = sockat.ex.resolve_for_connect(path)
try:
    client.connect(r.path)
finally:
    r.cleanup()
client.sendall(b"hello")
```

## Connect to the same peer again and again

```python
import socket

import sockat.ex

r = sockat.ex.resolve_for_connect(path)
try:
    for _ in range(3):
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(r.path)
            ...
finally:
    r.cleanup()
```

## Listen

When the bind or the listen fails, `cleanup()` before raising the error.

```python
import socket

import sockat.ex

listener = socket.socket(socket.AF_UNIX)
r = sockat.ex.resolve_for_bind(path)
try:
    listener.bind(r.path)
    listener.listen()
except OSError:
    r.cleanup()
    raise
file = r.socket_file()
```

## Remove the socket file when done

Call `file.remove()` before closing the listener.

```python
import socket

import sockat.ex

listener = socket.socket(socket.AF_UNIX)
r = sockat.ex.resolve_for_bind(path)
try:
    listener.bind(r.path)
    listener.listen()
except OSError:
    r.cleanup()
    raise
file = r.socket_file()
...
file.remove()
listener.close()
```

## Keep a spelling to use later

How long the spelling can be used is told by `spelling`.

```python
import sockat.ex

r = sockat.ex.resolve_for_connect(path)
match r.spelling:
    case sockat.ex.Original():
        ...  # usable as long as the path is
    case sockat.ex.Linked(until=sockat.ex.Until.PROCESS):
        ...  # usable while this process lives
    case _:
        ...  # usable until r.cleanup()
r.cleanup()
```
