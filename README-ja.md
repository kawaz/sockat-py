# sockat

> [English](./README.md) | 日本語

Unix domain socket のパスが長すぎて bind / connect できない。sockat は、それを無くす Python のライブラリです。

長いパスを Python の標準の `socket` に渡すと、bind も connect もこのエラーで断られます。asyncio の `start_unix_server(path=...)` と `open_unix_connection(path)` も同じです。

```text
OSError: AF_UNIX path too long
```

標準の書き方を sockat に置き換えるだけで、長いパスでもそのまま動きます。

```python
import sockat

# listener = socket.socket(socket.AF_UNIX); listener.bind(path); listener.listen()
listener = sockat.bind(path)

# client = socket.socket(socket.AF_UNIX); client.connect(path)
client = sockat.connect(path)

# asyncio では、作ったソケットを sock= に渡す
server = await asyncio.start_unix_server(handle, sock=sockat.bind(path))
reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

返ってくるのは標準と同じ `socket.socket` で、短いパスは今までどおり標準にそのまま渡します。長いパスの時だけ、sockat が同じ場所を指す短い道を裏で用意してつなぎます。パスを短くする工夫も、OS ごとの上限も、気にしなくて構いません。

## インストール

```sh
pip install sockat
```

Python 3.11 以上で動き、他のパッケージに依存しません。

## もっと詳しく

- [利用マニュアル](./docs/MANUAL-ja.md)
- [リファレンス](./docs/REFERENCE-ja.md)
- [技術説明](https://github.com/kawaz/sockat/blob/main/pages/technical-ja.md)

## ライセンス

MIT License, Yoshiaki Kawazu (@kawaz)
