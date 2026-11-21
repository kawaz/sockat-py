# sockat マニュアル

> [English](./MANUAL.md) | 日本語

sockat-py の Python での名前と型、使ってはいけない書き方の Python での形、Python に固有の注意。言語によらない振る舞い (経路、置き場に残る物、片付け、長いパスで標準と違う点、エラーの状況) は [sockat の仕様](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md) が正本で、ここには写さずリンクする。経路の作り方は [SPEC-routes](https://github.com/kawaz/sockat/blob/main/docs/SPEC-routes-ja.md)、どこまで届くかは[届く範囲の表](https://github.com/kawaz/sockat/blob/main/pages/reach/README-ja.md)、Python 版の設計の判断は [DR-0001](./decisions/DR-0001-python-port-design.md)。

## 使い方

```python
import sockat

path = "/とても/深い/ディレクトリ/.../server.sock"
listener = sockat.bind(path)
client = sockat.connect(path)
server, _ = listener.accept()
client.sendall(b"ping")
assert server.recv(4) == b"ping"
```

| 関数 | 置き換える標準の書き方 | 戻り値 |
|---|---|---|
| `sockat.bind(path)` | `socket()` → `bind(path)` → `listen()` | listen 済みの `socket.socket` |
| `sockat.connect(path)` | `socket()` → `connect(path)` | `socket.socket` |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | 非ブロッキングの `socket.socket` |
| `sockat.datagram.bind(path)` | `socket(AF_UNIX, SOCK_DGRAM)` → `bind(path)` | `socket.socket` |
| `sockat.datagram.unbound()` | `socket(AF_UNIX, SOCK_DGRAM)` | `socket.socket` |
| `sockat.datagram.connect(sock, path)` | `sock.connect(path)` | `None` |
| `sockat.ex.bind(path, options=...)` など | 同じ + 綴りの性質・片付け・診断 | `Bound` / `Connected` / `Report` ([sockat.ex](#sockatex)) |
| `sockat.ex.resolve_for_bind(path, options=...)` / `resolve_for_connect` | 標準に自分で渡す短い綴り | `ForBind` / `ForConnect` ([resolve](#resolve)) |
| `sockat.ex.sweep()` / `sweep_base(path)` | — | `None` ([sweep](#sweep)) |

- `path` は `str` / `bytes` / `os.PathLike`
- どのソケットも `socket.socket(AF_UNIX, SOCK_STREAM)` (DGRAM は `SOCK_DGRAM`) で作るので、標準と同じ設定になる (close-on-exec、`socket.setdefaulttimeout` の値、listen の既定の backlog)。backlog を変えたい時は、返った listener で `listen(n)` し直す
- ソケットを閉じてもファイルは消えない (Python の標準と同じ)。消す時は `sockat.ex.bind` の `file.remove()` か、元のパスで `os.unlink(path)` (unlink には `sun_path` の上限が無い)

## connect と connect_nonblocking

振る舞いは仕様の「[ブロックしない connect](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#ブロックしない-connect)」。Python では:

- `connect` は標準の `connect` と同じく `settimeout` の値に従う
- `connect_nonblocking` は、backlog が満杯なら Linux は `BlockingIOError`、macOS は `ConnectionRefusedError` で待たずに失敗する

## sockat.datagram

標準の DGRAM のうち、パスを取る関数と、それと組で使う `unbound` だけを写す ([仕様の「関数」](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#関数))。

```python
import sockat

server = sockat.datagram.bind(path)
client = sockat.datagram.unbound()
sockat.datagram.connect(client, path)
client.send(b"hello")
```

- `connect` は渡したソケットで経路を試す。失敗してもソケットは閉じない (閉じるのは利用者)
- `unbound()` が返すのは `socket.socket` の subclass ([Python に固有の注意](#python-に固有の注意))
- `send_to` と SEQPACKET はまだ無い (仕様で未確定)

## sockat.ex

`import sockat` では読み込まれない。使う人が `import sockat.ex` で選ぶ。`sockat.ex.connect(...)` のようにモジュールを通して呼ぶ ([Python に固有の注意](#python-に固有の注意))。

```python
import asyncio

import sockat.ex

bound = sockat.ex.bind(path)
server = await asyncio.start_unix_server(handle, sock=bound.socket)
...
bound.file.remove()  # before closing the listener
server.close()

connected = sockat.ex.connect(path)
```

| 関数 | 戻り値 |
|---|---|
| `sockat.ex.bind(path, *, options=None)` | `Bound(socket, file, spelling, route, base, skipped)` |
| `sockat.ex.connect(path, *, options=None)` / `connect_nonblocking` | `Connected(socket, route, skipped)` |
| `sockat.ex.datagram.bind(path, *, options=None)` | `Bound` (socket は DGRAM) |
| `sockat.ex.datagram.connect(sock, path, *, options=None)` | `Report(route, skipped)` |
| `sockat.ex.resolve_for_bind(path, *, options=None)` | `ForBind(path, spelling, route, base, skipped)` と `cleanup()`・`socket_file()` |
| `sockat.ex.resolve_for_connect(path, *, options=None)` | `ForConnect(path, route, skipped)` と `cleanup()` |
| `sockat.ex.sweep()` / `sockat.ex.sweep_base(path)` | `None` |

- `options` はキーワードでだけ渡せる (`sockat.ex.bind(path, options=sockat.ex.Options(...))`)
- `Bound` / `Connected` / `Report` は名前付きタプルで、上の順に分解できる。各項目の意味は仕様の「[返す物](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#返す物)」と「[診断](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#診断)」

### Options

キーワード専用の frozen dataclass。各項目の意味は仕様の「[`Options`](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#options)」。

| 項目 | 型 |
|---|---|
| `usable_cross_uid` | `bool = False` |
| `base` | `str \| bytes \| os.PathLike \| None = None`。絶対パスで名前で終わる物 (違えば `ValueError`) |

### 綴りの性質 (Spelling)

`Bound.spelling` / `ForBind.spelling` は `Spelling` の値。値と属性の意味は仕様の「[綴りの性質 `Spelling`](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#綴りの性質-spelling)」。

| 値 | Python の形 |
|---|---|
| `Original` | `sockat.ex.Original()` |
| `CwdRelative` | `sockat.ex.CwdRelative()` |
| `Linked` | `sockat.ex.Linked(removable, until, usable_cross_uid, usable_with_private_tmp)`。`until` は `sockat.ex.Until.PROCESS` / `Until.CLEANUP` (`StrEnum`、値は `"Process"` / `"Cleanup"`) |
| `Dangling` | `sockat.ex.Dangling()` |

値ごとの frozen dataclass なので、`match` のクラスパターンで属性ごと分岐できる。知らない値は「綴りを使えない」と読む。

```python
match bound.spelling:
    case sockat.ex.Linked(removable=True):
        ...  # unlinking by the spelling removes the socket file
    case sockat.ex.Original() | sockat.ex.CwdRelative():
        ...  # the path itself
    case _:
        ...  # it does not: remove by the path or file.remove()
```

### remove と cleanup

`Bound.file` は `SocketFile` で、`remove()` と `cleanup()` を持つ。何をするかと約束の範囲は仕様の「[片付け](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#片付け)」(**`remove()` は listener を閉じる前に呼ぶ**)。Python では:

- 失敗は `OSError` (その errno のサブクラス) で返す。`remove()` で両方が失敗した時は、ファイル側のエラーを返し、`cleanup()` のエラーはその例外の note (`add_note`) に足す
- 相対パスの `sockat.ex.bind` で、呼び出しの初めと覚えた後で cwd が違えば、errno の無い `OSError`
- 既定の `sockat.bind` は `file` を返さない。何が残り、長命のプロセスがどう片付けるかは仕様の「[置き場に残る物](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#置き場に残る物)」

### 診断とエラー

`route`・`base`・`skipped` の意味は仕様の「[診断](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#診断)」。Python では:

- `skipped` は `Skipped(name, reason)` (名前付きタプル) のタプル
- 失敗した時は、例外 (型と `errno` は既定の呼び出しと同じ) に属性 `skipped` と `route` を足す (包んだり別の型にしたりしない)

### resolve

振る舞いは仕様の「[resolve](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#resolve)」と「[`ForBind` の `SocketFile`](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#forbind-の-socketfile)」。綴り (`path`) は、渡したパスが `str` なら `str`、`bytes` なら `bytes`。

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

`ForBind` は `remove()` を持たない。自分で bind した後・listener を閉じる前に `socket_file()` を呼ぶと `SocketFile` を返す (bind の前はエラー: ファイルが無ければ `FileNotFoundError`、ソケットでなければ `ENOTSOCK` の `OSError`)。

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

### sweep

`sockat.ex.sweep()` と `sockat.ex.sweep_base(path)`。何を消し何を消さないかは仕様の「[`sweep`](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#sweep)」。sockat が自分で呼ぶことは無いので、試験を繰り返す環境では `sockat.ex.sweep()` を呼ぶ。

## Python に固有の注意

- **アドレスは元のパスを返す**: 長いパスでは、カーネルが返すアドレスは bind の綴り (仕様の「[長いパスで標準と違う点](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#長いパスで標準と違う点)」)。sockat が返すソケットは `socket.socket` の subclass で、listener (と accept したソケット) の `getsockname()` と、connect したソケットの `getpeername()` を元のパスに差し替える。標準のソケットが失敗する所 (閉じた後の `EBADF`、listener の `getpeername` の `ENOTCONN`) は同じく失敗する。短いパスでは素の `socket.socket` ([DR-0003](./decisions/DR-0003-report-the-given-path.md))
  - `isinstance(x, socket.socket)` は真だが、長いパスでは `type(x) is socket.socket` は偽
  - `socket.fromfd` 等で fd から作り直したソケットは標準の型なので、カーネルの綴りを返す
- **`sockat.datagram.unbound()` は subclass を返す**: 後で `sockat.datagram.connect` で長いパスに connect した時に、`getpeername()` を元のパスにするため (bind していないので `getsockname()` はカーネルの値のまま)。短いパスにつなぎ直すと、`getpeername()` はカーネルの値に戻る
- **asyncio の後始末**: Python 3.13 以降の `asyncio.start_unix_server(sock=)` は、server を閉じる時に listener の `getsockname()` のファイルを消す (`cleanup_socket`)。sockat の listener は元のパスを返すので、短いパスと同じに元のパスのソケットファイルが消える ([asyncio](#asyncio))
- **`sockat.ex` は別のモジュール**: `_ex` の関数は `sockat.ex` に置き、`import sockat` では読み込まない。`from sockat.ex import connect` と取り込むと、既定の `sockat.connect` や標準の `socket.socket.connect_ex` と紛れる
- **listener を閉じてもファイルは消えない**: Python の標準は listener を閉じてもソケットのファイルを消さない。sockat も消さない (`sockat.ex.bind` の listener は短いパスでも、消すのは `file.remove()` だけ)
- **`os.path.abspath`・`normpath`・`relpath` は `..` を字句で畳む**: `a/sym/../s.sock` (sym は symlink) を、カーネルは symlink の先の親と見るが、これらの関数は `a/s.sock` にする。相対パスを絶対パスにする時は `os.path.join(os.getcwd(), path)` にする。sockat 自身はディレクトリを `os.stat` と `os.path.realpath` (symlink を解いてから `..` を処理する) で引くので、カーネルと同じ所を指す

## 使ってはいけない書き方

言語によらない一覧と、なぜいけないかは仕様の「[使ってはいけない書き方](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#使ってはいけない書き方)」。ここには Python での書き方と、代わりの書き方のコードを置く。コードは試験 (`tests/test_docs.py`) がこの文書から読み出して動かす。`path` はソケットのパス。

### Spelling を見ずに、カーネルが返す綴りで消す

- 書き方: `os.unlink(socket.socket.getsockname(listener))` のように、カーネルが返す綴りでソケットのファイルを unlink / stat する
- 代わりに: 元のパスを持っておいて使う。`file.remove()`

```python
import os

import sockat

listener = sockat.bind(path)
...
listener.close()
os.unlink(path)
```

### cleanup の後やプロセスが終わった後に、古い綴りを使う

- 書き方: `cleanup()` の後に resolve の綴り (`ForConnect.path` 等) で connect する。プロセスが終わった後に、そのプロセスの bind の綴りを使う
- 代わりに: 綴りは `until` の間だけ使う

```python
import socket

import sockat.ex

bound = sockat.ex.bind(path)
r = sockat.ex.resolve_for_connect(path)
for _ in range(3):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(r.path)
r.cleanup()
bound.file.remove()
bound.socket.close()
```

### 相対パスで bind し、cwd を変えた後にその相対パスで消す

- 書き方: `sockat.bind("server.sock")` の後に `os.chdir(...)` し、`os.unlink("server.sock")` で消す
- 代わりに: bind の前に cwd を前に付けて絶対パスにする (`os.path.abspath` は `..` を字句で畳むので使わない。[Python に固有の注意](#python-に固有の注意))。`file.remove()`

```python
import os

import sockat.ex

path = os.path.join(os.getcwd(), path)
bound = sockat.ex.bind(path)
os.chdir("/")
...
bound.file.remove()
bound.socket.close()
```

### listener を閉じればソケットのファイルが消えると当てにする

- 書き方: `listener.close()` だけで後始末を終える
- 代わりに: `file.remove()` か、元のパスで消す

```python
import sockat.ex

bound = sockat.ex.bind(path)
...
bound.file.remove()
bound.socket.close()
```

### remove() で listener も閉じると当てにする

- 書き方: `file.remove()` だけを呼んで listener を閉じない
- 代わりに: listener は自分で閉じる

```python
import sockat.ex

bound = sockat.ex.bind(path)
...
bound.file.remove()
bound.socket.close()
```

### listener を閉じた後に remove() を呼ぶ

- 書き方: `bound.socket.close()` の後に `bound.file.remove()` を呼ぶ
- 代わりに: listener を閉じる前に呼ぶ

```python
import sockat.ex

bound = sockat.ex.bind(path)
...
bound.file.remove()
bound.socket.close()
```

### procB の綴りや usable_cross_uid が False の綴りを別の uid の相手に辿らせる

- 書き方: 既定の bind のカーネルの綴りを、別の uid で動く相手に渡して辿らせる
- 代わりに: `Options(usable_cross_uid=True)` を指定し、結果の `spelling` を確かめる

```python
import sockat.ex

bound = sockat.ex.bind(path, options=sockat.ex.Options(usable_cross_uid=True))
match bound.spelling:
    case sockat.ex.Linked(usable_cross_uid=True):
        ...  # a peer of another uid can follow the kernel's spelling
    case _:
        ...  # it cannot: give the peer the path itself
bound.file.remove()
bound.socket.close()
```

### route・base・skipped・reason の文字列で分岐する

- 書き方: `if bound.route == "link-file":` のように、`sockat.ex` の `route`・`base`・`skipped`・`reason` の文字列で分岐する
- 代わりに: `spelling` で分岐する

```python
import sockat.ex

bound = sockat.ex.bind(path)
match bound.spelling:
    case sockat.ex.Linked(removable=True):
        ...  # unlinking by the spelling removes the socket file
    case _:
        ...  # it does not: remove by the path or file.remove()
bound.file.remove()
bound.socket.close()
```

## エラー

状況ごとの errno は仕様の「[エラー](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#エラー)」。sockat は OS のエラーを書き換えないので、OS の errno の値はその OS の標準と同じ。Python での型:

| 仕様の状況 | Python の型 |
|---|---|
| OS のエラー | errno 付きの `OSError` (`FileNotFoundError`・`NotADirectoryError`・`ConnectionRefusedError`・`BlockingIOError`・`PermissionError` 等、その errno のサブクラス) |
| 引数の誤り (空のパス、先頭以外に NUL、名前で終わらない長いパス、`Options.base` の形の誤り) | `ValueError` |
| 一般のエラー (relative の途中、または相対パスの `sockat.ex.bind` の途中で cwd が変わった) | errno の無い `OSError` |

## asyncio

戻り値は標準の `socket.socket` なので、`sock=` を受け取る API にそのまま渡せる。

```python
server = await asyncio.start_unix_server(handle, sock=sockat.bind(path))

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- 引数を足さずに、短いパスと同じに動く。Python 3.13 以降の asyncio は、server を閉じる時に元のパスのソケットファイルを消す。3.11 / 3.12 の asyncio は消さない (標準と同じ)
- transport の `get_extra_info('sockname')` (server 側) / `get_extra_info('peername')` (client 側) も元のパス
- `sockat.ex` の `socket` も同じに渡せる。`file.remove()` は server を閉じる前 (動作中) に呼ぶ
- `connect_nonblocking` は相手を待たないので、イベントループから直接呼べる。`bind` も相手を待たない (thread-cwd ではスレッドの終わりを待つが、bind してすぐ終わる)

## 確かめていないこと

- Android と iOS では動かしていない
- FreeBSD・NetBSD・OpenBSD・illumos は CI の VM で試験を回している
- 言語によらない未確定は仕様の「[未確定](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#未確定)」
