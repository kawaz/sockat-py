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
| `sockat.bind_ex(path)` | 同上 + ソケットファイルの後始末 | `(listener, discard)` ([後述](#後始末つきの-bind)) |
| `sockat.connect(path)` | `socket()` → `connect(path)` | `socket.socket` |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | 非ブロッキングの `socket.socket` |

- 戻り値は標準の `socket.socket` (長いパスでは、アドレスを元のパスで返す `socket.socket` の subclass)
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

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` を使える) | スレッド cwd → /proc 版 | スレッド cwd → /proc 版 → /proc → fd で掴む形 → symlink |
| Linux / Android (`/proc` を使えない) | スレッド cwd | スレッド cwd → 相対パス → symlink |
| FreeBSD | bindat | connectat → connectat (ソケットのディレクトリ) |
| macOS / iOS | スレッド cwd | スレッド cwd → 相対パス → 広げた sockaddr → symlink |
| NetBSD / OpenBSD / illumos / その他の Unix | なし (`ENAMETOOLONG`) | 相対パス → symlink |
| Windows | 対象外 (import で `ImportError`) | 同左 |

| 経路 | やり方 |
|---|---|
| 中心の経路 | 自分専用のディレクトリ D を作って fd で掴み、その中にソケットのディレクトリを指す乱数の symlink R を作り、D を基準に `R/<ファイル名>` で渡して、R と D を消す。D は `TMPDIR`、`/tmp`、ソケットのディレクトリの順に、作れた所に作る |
| スレッド cwd | 中心の経路の手段。使い捨てのスレッドだけ cwd を D に移す (Linux / Android、macOS / iOS) |
| bindat / connectat | 中心の経路の手段。`bindat(2)` / `connectat(2)` に D の fd を渡す (FreeBSD) |
| /proc 版 | 中心の経路の手段。`/proc/self/fd/<D の fd>/R/<ファイル名>` で渡す (Linux / Android) |
| D 無しの形 | bind だけ。D をどこにも作れない時、ソケットのディレクトリに R (`.sockat.XXXXXXXXXX`) を作り、ソケットのディレクトリを基準に同じ手段で `.sockat.XXXXXXXXXX/<ファイル名>` を渡して、R を消す |
| /proc | connect だけ。`/proc/self/fd/<ソケットのディレクトリの fd>/<ファイル名>` で渡す |
| fd で掴む形 | connect だけ。D の中にソケットのファイルを指す symlink `s` を作り、`/proc/self/fd/<D の fd>/s` で渡す |
| 相対パス | connect だけ。cwd からの相対パスで渡す。文字列を書き直すだけで cwd は変えない |
| 広げた sockaddr | connect だけ。macOS のカーネルが受け付ける、253 バイトまで入る sockaddr で渡す |
| symlink | connect だけ。他人が差し替えられない `TMPDIR` か `/tmp` に、ソケットのファイルを指す短い symlink を作って通し、すぐ消す |
| connectat (ソケットのディレクトリ) | connect だけ。`connectat(2)` にソケットのディレクトリの fd とファイル名を渡す (FreeBSD) |
| fork | **無い** (仕様の fork の経路は作っていない。仕様の `connect_any` 系も無い) |

- どの経路も呼び出し元のプロセスとスレッドの cwd を変えないので、マルチスレッドのプログラムから使える
- bind はディレクトリの深さによらず、ファイル名が 96 バイト (macOS / FreeBSD は 92、/proc 版はおよそ 79) まで届く (`R/` の分、標準より短い)。D をどこにも作れない時は D 無しの形で 88 バイト (macOS / FreeBSD は 84、/proc 版はおよそ 72) まで
- NetBSD / OpenBSD / illumos と、中心の経路の手段を使えない環境 (seccomp の filter の下で `/proc` の無い Linux 等) では、長いパスの bind は `ENAMETOOLONG` になる (仕様では fork の経路で届く所。ハブの D12-Q3 で裁定待ち)
- どの経路にも収まらないパスは、標準で長すぎるパスと同じく `ENAMETOOLONG` (errno 付きの `OSError`) になる
- 各経路の条件と、どこから届かなくなるかは [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#長いパスへの届き方) にある

## 長いパスで標準と違う点

- listener (と accept したソケット) の `getsockname()` と、connect したソケットの `getpeername()` は元のパスを返す。カーネルが返すのは `R/<ファイル名>` (/proc 版は `/proc/self/fd/<fd>/R/<ファイル名>`、D 無しの形は R が `.sockat.XXXXXXXXXX`。R はもう無いので何にも当たらない) だが、sockat が返すソケットはそれを元のパスに差し替える
- 標準にそのまま渡せない長さのパスで、名前で終わらないもの (`dir/`、`dir/.`、`..` で終わるもの) は `ValueError` になる
- 先頭以外に NUL を含むパスは、長さによらず `ValueError` になる (標準は NUL で黙って切り詰める)。Linux で先頭が NUL のパス (abstract namespace) は標準にそのまま渡す
- 途中でプロセスが死ぬと、`TMPDIR` か `/tmp` に `sockat.XXXXXXXXXX`、ソケットのディレクトリに `.sockat.XXXXXXXXXX` が残ることがある
- その他 (ディレクトリを先に引くこと等) は [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#長いパスで標準と違う点)

## 後始末つきの bind

```python
listener, discard = sockat.bind_ex(path)
...
listener.close()
discard()
```

- `discard()` は bind が作ったソケットファイルだけを消す (別のファイルに置き換わっていたら消さない)。何度呼んでもよい
- `discard()` は listener を閉じない
- 名前は仮。詳細は [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#bind_ex)

## asyncio

```python
listener = sockat.bind(path)
server = await asyncio.start_unix_server(handle, sock=listener)

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- 引数を足さずに短いパスと同じに動く (Python 3.13 以降は server を閉じる時に元のパスのソケットファイルを消す)

## ドキュメント

- [docs/MANUAL-ja.md](./docs/MANUAL-ja.md) — 使い方、経路の詳細、標準と違う点、エラー
- [docs/decisions/](./docs/decisions/INDEX.md) — 設計の判断 (DR)

## ライセンス

MIT License, Yoshiaki Kawazu (@kawaz)
