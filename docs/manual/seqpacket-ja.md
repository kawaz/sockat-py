# SEQPACKET を使う

> [English](./seqpacket.md) | 日本語

SEQPACKET の関数はまだ無いので、resolve で作った綴りを自分で作った `socket.socket` に渡す。macOS では標準が SEQPACKET のソケットを作れないので使えない。

## 待ち受ける

```python
import socket

import sockat.ex

r = sockat.ex.resolve_for_bind(path)
listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
listener.bind(r.path)
listener.listen()
file = r.socket_file()
```

## つなぐ

```python
import socket

import sockat.ex

r = sockat.ex.resolve_for_connect(path)
client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
client.connect(r.path)
r.cleanup()
client.send(b"hello")
```

## 終わる時にソケットのファイルを消す

`file.remove()` は listener を閉じる前に呼ぶ。

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
