# つなぐ (connect)

> [English](./connect.md) | 日本語

## つなぐ

```python
import sockat

client = sockat.connect(path)
client.sendall(b"hello")
```

## asyncio でつなぐ

`connect_nonblocking` は相手を待たないので、イベントループから呼べる。

```python
import asyncio

import sockat

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

## 時間を区切ってつなぐ

```python
import socket

import sockat

socket.setdefaulttimeout(5)
client = sockat.connect(path)
```
