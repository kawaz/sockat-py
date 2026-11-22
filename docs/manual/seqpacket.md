# Using SEQPACKET

> English | [日本語](./seqpacket-ja.md)

There are no SEQPACKET functions yet, so pass the spelling a resolve makes to a `socket.socket` you make yourself. It cannot be used on macOS, where the standard library cannot make a SEQPACKET socket.

## Listen

```python
import socket

import sockat.ex

r = sockat.ex.resolve_for_bind(path)
listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
listener.bind(r.path)
listener.listen()
file = r.socket_file()
```

## Connect

```python
import socket

import sockat.ex

r = sockat.ex.resolve_for_connect(path)
client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
client.connect(r.path)
r.cleanup()
client.send(b"hello")
```

## Remove the socket file when done

Call `file.remove()` before closing the listener.

```python
import socket

import sockat.ex

r = sockat.ex.resolve_for_bind(path)
listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
listener.bind(r.path)
listener.listen()
file = r.socket_file()
...
file.remove()
listener.close()
```
