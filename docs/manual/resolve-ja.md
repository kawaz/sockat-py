# パスだけを解決する (resolve)

> [English](./resolve.md) | 日本語

bind / connect は自分の書き方 (標準の `socket`、他のライブラリ) のまま、パスだけを上限に収まる綴りにする。

## 何度もつなぐ綴りを作る

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

## 自分で bind する綴りを作り、終わる時にソケットのファイルを消す

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
