# Resolving just the path (resolve)

> English | [日本語](./resolve-ja.md)

Keep your own way of binding / connecting (the standard `socket`, another library), and make only the path a spelling that fits the limit.

## Make a spelling to connect to again and again

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

## Make a spelling to bind yourself, and remove the socket file when done

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
