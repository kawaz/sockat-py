# Connecting (connect)

> English | [日本語](./connect-ja.md)

## Connect

```python
import sockat

client = sockat.connect(path)
client.sendall(b"hello")
```

## Connect with asyncio

`connect_nonblocking` does not wait for the peer, so it can be called from the event loop.

```python
import asyncio

import sockat

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

## Connect with a time limit

```python
import socket

import sockat

socket.setdefaulttimeout(5)
client = sockat.connect(path)
```
