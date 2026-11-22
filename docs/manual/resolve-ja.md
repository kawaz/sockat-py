# パスだけを解決する (resolve)

> [English](./resolve.md) | 日本語

bind / connect は自分の書き方 (標準の `socket`、他のライブラリ) のまま、パスだけを上限に収まる綴りにする。

## つなぐ

綴りは connect の後は要らないので、結果によらずすぐ `cleanup()` する。

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

## 同じ相手に何度もつなぐ

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

## 待ち受ける

bind や listen に失敗したら、`cleanup()` してからエラーを返す。

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

## 終わる時にソケットのファイルを消す

`file.remove()` は listener を閉じる前に呼ぶ。

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

## 綴りを取っておいて後で使う

綴りをいつまで使えるかは `spelling` で分かる。

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
