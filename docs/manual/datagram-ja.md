# DGRAM を使う

> [English](./datagram.md) | 日本語

## 受け取る側を bind する

```python
import sockat

server = sockat.datagram.bind(path)
```

## 送る側をつなぐ

```python
import sockat

server = sockat.datagram.bind(path)
client = sockat.datagram.unbound()
sockat.datagram.connect(client, path)
client.send(b"hello")
assert server.recv(5) == b"hello"
```

## 終わる時にソケットのファイルを消す

```python
import sockat.ex.datagram

bound = sockat.ex.datagram.bind(path)
...
bound.file.remove()
bound.socket.close()
```
