# sockat

> [English](./README.md) | 日本語

`sun_path` の上限 (NUL 込みで macOS と BSD は 104 バイト、Linux は 108 バイト) を超えるパスでも、Unix domain socket を bind / connect できる Python のライブラリ。

言語によらない振る舞いの正本は [sockat の仕様](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md) で、sockat-py はその Python 実装。

```python
import sockat

listener = sockat.bind("/とても/深い/ディレクトリ/.../server.sock")
client = sockat.connect("/とても/深い/ディレクトリ/.../server.sock")
```

## 使い方

```sh
pip install sockat
```

`socket.socket(AF_UNIX, SOCK_STREAM)` を作って `bind` + `listen` / `connect` する代わりに、`sockat.bind` / `sockat.connect` を使う。asyncio 等のイベントループでは、待たずに非ブロッキングのソケットを返す `sockat.connect_nonblocking` を使う。

| 関数 | 置き換える標準の書き方 | 戻り値 |
|---|---|---|
| `sockat.bind(path)` | `socket()` → `bind(path)` → `listen()` | listen 済みの `socket.socket` |
| `sockat.connect(path)` | `socket()` → `connect(path)` | `socket.socket` |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | 非ブロッキングの `socket.socket` |

- 戻り値は標準の `socket.socket` そのもの
- 標準が受け付ける長さのパスは標準にそのまま渡すので、標準と同じに振る舞う
- 長いパスでも、ソケットファイルは渡したパスにできる
- `path` は `str` / `bytes` / `os.PathLike`
- Python 3.11 以上。依存なし (純 Python)

## なぜ要るか

標準の `socket.bind` / `socket.connect` は `sun_path` に収まらないパスを `OSError('AF_UNIX path too long')` で断る。ファイルシステムには上限が無いのに、深い一時ディレクトリ (macOS の `$TMPDIR` はそれだけで 50 バイト前後、試験の枠組みやサンドボックスがさらに掘る) にソケットを置くとすぐに当たる。上限が効くのは `bind(2)` / `connect(2)` に渡す引数の長さなので、sockat は同じ場所を指す短い綴りでカーネルに渡す。

## 長いパスへの届き方

標準が受け付ける長さ (`sun_path` − 1) のパスは標準にそのまま渡す。

| OS | 標準にそのまま渡す長さ |
|---|---|
| Linux / Android / illumos | 107 バイトまで |
| macOS / iOS / FreeBSD / NetBSD / OpenBSD | 103 バイトまで |

それを超えるパスは、OS ごとに次の順に試し、最初に使えた経路で bind / connect する。

| OS | 試す順番 |
|---|---|
| Linux / Android (`/proc` を使える) | /proc |
| Linux / Android (`/proc` を使えない) | 相対パス → symlink → スレッド cwd |
| FreeBSD | bindat |
| macOS / iOS | 相対パス → 広げた sockaddr (connect だけ) → symlink → スレッド cwd |
| NetBSD / OpenBSD / illumos / その他の Unix | 相対パス → symlink |
| Windows | 対象外 (import で `ImportError`) |

| 経路 | やり方 |
|---|---|
| /proc | ディレクトリを開き、`/proc/self/fd/<fd>/<ファイル名>` で渡す |
| bindat | `bindat(2)` / `connectat(2)` で、ディレクトリの fd を基準にファイル名で渡す |
| 相対パス | cwd からの相対パスで渡す。文字列を書き直すだけで cwd は変えない |
| 広げた sockaddr | macOS のカーネルが受け付ける、253 バイトまで入る sockaddr で渡す。connect だけが使う (bind に使うと、listener のアドレスを標準の `getsockname` が読み過ぎる) |
| symlink | `TMPDIR` か `/tmp` に短い symlink を作って通し、すぐ消す。他人が差し替えられない置き場にだけ作る。`/proc` を使える Linux の connect は、安全でない置き場でも自分専用のディレクトリを作って fd で掴んで使う |
| スレッド cwd | 使い捨てのスレッドだけ cwd をディレクトリに移し、ファイル名で渡す。プロセスの cwd は変えない |
| fork | **無い** (仕様の fork の経路は作っていない。仕様の `connect_any` 系も無い) |

- どの経路も呼び出し元のプロセスの cwd を変えないので、マルチスレッドのプログラムから使える
- どの経路にも収まらないパスは、標準で長すぎるパスと同じく `ENAMETOOLONG` (errno 付きの `OSError`) になる
- スレッド cwd の経路を使える macOS / Linux では、ファイル名が 103 バイト (Linux は 107) 以内ならどの深さでも届く (macOS の connect は 253 バイトまで)。seccomp の filter の下 (docker の既定等) の Linux ではスレッド cwd の経路を使えない
- 各経路の長さの上限と、どこから届かなくなるかは [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#長いパスへの届き方) にある

## 長いパスで標準と違う点

- `getsockname()` / `getpeername()` は元のパスを返さず、bind の時にカーネルに渡した綴り (相対パス、`/proc/self/fd/...`、もう無い symlink を通したパス、ファイル名だけ等) を返す。元のパスは自分で持っておく
- 標準にそのまま渡せない長さのパスで、名前で終わらないもの (`dir/`、`dir/.`、`..` で終わるもの) は `ValueError` になる
- 先頭以外に NUL を含むパスは、長さによらず `ValueError` になる (標準は NUL で黙って切り詰める)。Linux で先頭が NUL のパス (abstract namespace) は標準にそのまま渡す
- その他 (ディレクトリを先に引くこと、symlink が残りうること) は [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#長いパスで標準と違う点)

## asyncio

```python
listener = sockat.bind(path)
server = await asyncio.start_unix_server(handle, sock=listener, cleanup_socket=False)  # 3.13 以降

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

Python 3.13 以降の asyncio は、閉じる時に listener の `getsockname()` の綴りでソケットファイルを消そうとする。長いパスではその綴りが元のパスでないので、`cleanup_socket=False` を渡し、ソケットファイルは自分で消す ([詳しく](./docs/MANUAL-ja.md#asyncio))。

## ドキュメント

- [docs/MANUAL-ja.md](./docs/MANUAL-ja.md) — 使い方、経路の詳細、標準と違う点、エラー
- [docs/decisions/](./docs/decisions/INDEX.md) — 設計の判断 (DR)

## ライセンス

MIT License, Yoshiaki Kawazu (@kawaz)
