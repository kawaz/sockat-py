# 待ち受ける (bind)

> [English](./bind.md) | 日本語

## 待ち受ける

```python
import sockat

listener = sockat.bind(path)
```

## asyncio で待ち受ける

```python
import asyncio

import sockat

server = await asyncio.start_unix_server(handle, sock=sockat.bind(path))
```

## 終わる時にソケットのファイルを消す

`file.remove()` は listener を閉じる前に呼ぶ。

```python
import sockat.ex

bound = sockat.ex.bind(path)
...
bound.file.remove()
bound.socket.close()
```

## asyncio で、終わる時にソケットのファイルを消す

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

## 置き場を自分で決める

sockat が裏で作るディレクトリを、アプリが管理するディレクトリの下に置く (`base` の親は先に作っておく)。

```python
import sockat.ex

# base: a directory the application manages, such as "/run/myapp/sockat"
bound = sockat.ex.bind(path, options=sockat.ex.Options(base=base))
...
bound.file.remove()
bound.socket.close()
```

## 別の uid の相手につながせる

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

## 試験を繰り返す環境で、裏で作った物を片付ける

```python
import sockat.ex

sockat.ex.sweep()
```
