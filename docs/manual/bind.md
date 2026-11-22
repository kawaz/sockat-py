# Listening (bind)

> English | [日本語](./bind-ja.md)

## Listen

```python
import sockat

listener = sockat.bind(path)
```

## Listen with asyncio

```python
import asyncio

import sockat

server = await asyncio.start_unix_server(handle, sock=sockat.bind(path))
```

## Remove the socket file when done

Call `file.remove()` before closing the listener.

```python
import sockat.ex

bound = sockat.ex.bind(path)
...
bound.file.remove()
bound.socket.close()
```

## Remove the socket file when done, with asyncio

```python
import asyncio

import sockat.ex

bound = sockat.ex.bind(path)
server = await asyncio.start_unix_server(handle, sock=bound.socket)
...
bound.file.remove()
server.close()
await server.wait_closed()
```

## Choose where sockat keeps its directories

Put the directories sockat makes behind the scenes under a directory the application manages (make the parent of `base` first).

```python
import sockat.ex

# base: a directory the application manages, such as "/run/myapp/sockat"
bound = sockat.ex.bind(path, options=sockat.ex.Options(base=base))
...
bound.file.remove()
bound.socket.close()
```

## Let a peer of another uid connect

```python
import sockat.ex

bound = sockat.ex.bind(path, options=sockat.ex.Options(usable_cross_uid=True))
match bound.spelling:
    case sockat.ex.Linked(usable_cross_uid=True) | sockat.ex.Original():
        ...  # a peer of another uid can follow the address the kernel reports
    case _:
        ...  # give the peer the path itself
bound.file.remove()
bound.socket.close()
```

## Clean up what was made behind the scenes, where tests run over and over

```python
import sockat.ex

sockat.ex.sweep()
```
