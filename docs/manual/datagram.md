# Using DGRAM

> English | [日本語](./datagram-ja.md)

## Bind the receiving side

```python
import sockat

server = sockat.datagram.bind(path)
```

## Connect the sending side

```python
import sockat

server = sockat.datagram.bind(path)
client = sockat.datagram.unbound()
sockat.datagram.connect(client, path)
client.send(b"hello")
assert server.recv(5) == b"hello"
```

## Remove the socket file when done

```python
import sockat.ex.datagram

bound = sockat.ex.datagram.bind(path)
...
bound.file.remove()
bound.socket.close()
```
