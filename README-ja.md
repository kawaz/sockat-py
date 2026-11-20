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

`socket.socket(AF_UNIX, ...)` を作って `bind` (+ `listen`) / `connect` する代わりに、sockat の関数を使う。asyncio 等のイベントループでは、待たずに非ブロッキングのソケットを返す `sockat.connect_nonblocking` を使う。

| 関数 | 置き換える標準の書き方 | 戻り値 |
|---|---|---|
| `sockat.bind(path)` | `socket()` → `bind(path)` → `listen()` | listen 済みの `socket.socket` |
| `sockat.connect(path)` | `socket()` → `connect(path)` | `socket.socket` |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | 非ブロッキングの `socket.socket` |
| `sockat.datagram.bind(path)` / `unbound()` / `connect(sock, path)` | `SOCK_DGRAM` の同じ書き方 | `socket.socket` / `None` |
| `sockat.ex.bind(path, options)` など | 同じ + 綴りの性質・片付け・診断 | `Bound` / `Connected` / `Report` ([後述](#片付け診断つきの呼び出し-sockatex)) |
| `sockat.ex.resolve_for_bind` / `resolve_for_connect` | 標準に自分で渡す短い綴り | `ForBind` / `ForConnect` |

- 戻り値は標準の `socket.socket` (長いパスでは、アドレスを元のパスで返す `socket.socket` の subclass)
- 標準が受け付ける長さのパスは標準にそのまま渡すので、標準と同じに振る舞う
- 長いパスでも、ソケットファイルは渡したパスにできる
- `path` は `str` / `bytes` / `os.PathLike`
- Python 3.11 以上。依存なし (純 Python)

## なぜ要るか

標準の `socket.bind` / `socket.connect` は `sun_path` に収まらないパスを `OSError('AF_UNIX path too long')` で断る。ファイルシステムには上限が無いのに、深い一時ディレクトリ (macOS の `$TMPDIR` はそれだけで 50 バイト前後、試験の枠組みやサンドボックスがさらに掘る) にソケットを置くとすぐに当たる。上限が効くのは `bind(2)` / `connect(2)` に渡す引数の長さなので、sockat は同じ場所を指す短い綴りでカーネルに渡す。

## 長いパスへの届き方

標準が受け付ける長さ (`sun_path` − 1: Linux / Android / illumos は 107、macOS / iOS / BSD は 103 バイト) のパスは標準にそのまま渡す (経路 standard)。それを超えるパスは、OS ごとに次の順に経路を試し、最初に使えた経路で bind / connect する。経路は[仕様の「経路の名前」](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#経路の名前)で呼ぶ。

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` を使える) | link-dir (procB → B の実パス) → thread-cwd | proc-direct → thread-cwd-direct → link-file |
| Linux / Android (`/proc` を使えない) | link-dir (B の実パス) → thread-cwd | relative → thread-cwd-direct → link-file |
| macOS / iOS | link-dir (B の実パス) → link-file → thread-cwd | relative → widened → thread-cwd-direct → link-file |
| FreeBSD | link-dir (B の実パス) → at | at-direct → link-file |
| NetBSD | link-dir (B の実パス) → link-file | relative → link-file |
| OpenBSD / illumos | link-dir (B の実パス) | relative → link-file |
| その他の Unix | link-dir (B の実パス) → link-file (辿るかを実行時に見分ける) | relative → link-file |
| Windows | 対象外 (import で `ImportError`) | 同左 |

| 経路 | やり方 |
|---|---|
| link-dir | 置き場の B (`/tmp/sockat-<uid>` 等) の中のプロセスごとのディレクトリ D に、ソケットのディレクトリを指す symlink E を作り、`B/D/E/<ファイル名>` で bind する。B は `/proc/<pid>/fd/<B の fd>` (procB) か B の実パスで綴る。E は片付けるまで残すので、綴りは元のパスを指し続ける |
| link-file | ソケットのファイルを指す E を通す。綴りにファイル名が出ないので、ファイル名の長さによらず届く。bind は最後の要素の symlink を辿る OS (macOS と NetBSD で確認) でだけ。既定の connect は B の直下の E を通してすぐ消す |
| thread-cwd | D の中の乱数の E を、使い捨てのスレッドだけ cwd を D に移して `E/<ファイル名>` で bind し、すぐ消す (Linux / Android、macOS / iOS) |
| at | thread-cwd と同じ綴りを `bindat(2)` に D の fd で渡す (FreeBSD) |
| proc-direct | connect だけ。`/proc/self/fd/<ソケットのディレクトリの fd>/<ファイル名>` |
| relative | connect だけ。cwd からの相対パス (文字列を書き直すだけで cwd は変えない) |
| widened | connect だけ。macOS のカーネルが受け付ける、253 バイトまで入る sockaddr |
| at-direct | connect だけ。`connectat(2)` にソケットのディレクトリの fd とファイル名 (FreeBSD) |
| thread-cwd-direct | connect だけ。使い捨てのスレッドだけ cwd をソケットのディレクトリに移してファイル名で渡す |

- どの経路も呼び出し元のプロセスとスレッドの cwd を変えないので、マルチスレッドのプログラムから使える。fork はしない
- connect は書き込みもスレッドも要らない経路を先に試し、何も残さない
- どの経路にも収まらないパスは、標準で長すぎるパスと同じく `ENAMETOOLONG` (errno 付きの `OSError`) になる
- 置き場 B・D・E と、各経路の条件・どこまで届くかは [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#長いパスへの届き方) にある

## 長いパスで標準と違う点

- listener (と accept したソケット) の `getsockname()` と、connect したソケットの `getpeername()` は元のパスを返す。カーネルが返すのは bind の綴りだが、sockat が返すソケットはそれを元のパスに差し替える
- 状態がアプリの置き場の外に増える: B・D・E が `/tmp`・`$XDG_RUNTIME_DIR`・`$TMPDIR` に残り、既定の `bind` の E と死んだプロセスの D は OS の一時ディレクトリの掃除まで残る (`sockat.ex.sweep()` で死んだプロセスの D を消せる)。どこにも B を作れずソケットのディレクトリに作った B (`.sockat.XXXXXXXXXX`) と、確かめに外れて横に作った B (`sockat-<uid>.XXXXXXXXXX`) は、`sweep()` でも OS の掃除でも消えずに残りうる
- 片付けた後の綴りは使わない (`/tmp` の B は消えた後に他人が作り直しうる)
- 相手の `/tmp` が隔離されていると (systemd の `PrivateTmp` 等)、`/tmp` を通る綴りを相手は辿れない
- systemd v242 より古い Linux と、B が macOS の `$TMPDIR` にある時は、生きている D が消されうる (綴りが辿れなくなるだけ)
- 標準にそのまま渡せない長さのパスで、名前で終わらないもの (`dir/`、`dir/.`、`..` で終わるもの) は `ValueError` になる
- 先頭以外に NUL を含むパスは、長さによらず `ValueError` になる (標準は NUL で黙って切り詰める)。Linux で先頭が NUL のパス (abstract namespace) は標準にそのまま渡す
- その他 (ディレクトリを先に引くこと等) は [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#長いパスで標準と違う点)

## 片付け・診断つきの呼び出し (sockat.ex)

```python
import sockat.ex

bound = sockat.ex.bind(path)
...
bound.file.remove()
bound.socket.close()

connected = sockat.ex.connect(path)
```

- `sockat.ex.bind` / `connect` / `connect_nonblocking` (と `sockat.ex.datagram.bind` / `connect`) は、既定の呼び出しと同じ経路を試し、綴りの性質 (`spelling`)、通った経路 (`route`)、通った B (`base`)、試して使えなかった経路 (`skipped`) を一緒に返す
- `sockat.ex.connect(...)` のようにモジュールを通して呼ぶ。`sockat.ex` は `import sockat` では読み込まれない
- `Options(usable_cross_uid=True)` で別の uid の相手も辿れる綴りを (bind 系だけ)、`Options(base=...)` で B の場所をアプリが指定する (`sockat.ex.sweep_base(path)` でその B の死んだ D を消せる)
- `spelling` は `Original` / `CwdRelative` / `Linked(removable, until, usable_cross_uid, usable_with_private_tmp)` / `Dangling` のどれか。分岐にはこれを使う。`route`・`base`・`skipped` は診断用で中身は約束しない
- `file.remove()` は bind が作ったソケットファイルだけを消し (listener を開いている間に別のファイルに置き換わっていたら消さない)、綴りの E も片付ける。`file.cleanup()` は E だけを片付ける。どちらも 1 回だけ働き、listener は閉じない。`remove()` は listener を閉じる前に呼ぶ
- `resolve_for_bind` / `resolve_for_connect` は、標準に自分で渡す短い綴りを返す (同じパスへの connect を繰り返す時に綴りを使い回す等)。`resolve_for_bind` の綴りに自分で bind した後は、`socket_file()` で `SocketFile` を受け取って `remove()` できる
- 失敗した時は、例外 (型と `errno` は既定の呼び出しと同じ) に `skipped` と `route` の属性を付ける
- 詳細は [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#sockatex)

## 使ってはいけない書き方

無関係なファイルを消したり、ファイルを消し損ねたりする書き方。詳細と代わりの書き方のコードは [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#使ってはいけない書き方) にある (コードは試験として動かしている)。

| 書き方 | 代わりに |
|---|---|
| `os.unlink(socket.socket.getsockname(listener))` (`spelling` を見ずにカーネルが返す綴りで消す) | `os.unlink(path)` (元のパス) か `file.remove()` |
| `cleanup()` の後やプロセスが終わった後に古い綴りを使う | 綴りは `until` の間だけ使う |
| `sockat.bind("server.sock")` → `os.chdir(...)` → `os.unlink("server.sock")` | bind の前に `os.path.abspath(path)`。`file.remove()` |
| `listener.close()` だけで後始末を終える | `file.remove()` か `os.unlink(path)` |
| `file.remove()` だけを呼び listener を閉じない | listener も `close()` する |
| listener を閉じた後に `file.remove()` を呼ぶ | 閉じる前に呼ぶ |
| 既定の bind の綴りを別の uid の相手に辿らせる | `Options(usable_cross_uid=True)` を指定し、`spelling` を確かめる |
| `if bound.route == "link-file":` (`route`・`base`・`skipped`・`reason` で分岐する) | `match bound.spelling:` (`spelling` で分岐する) |

## asyncio

```python
listener = sockat.bind(path)
server = await asyncio.start_unix_server(handle, sock=listener)

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- 引数を足さずに短いパスと同じに動く (Python 3.13 以降は server を閉じる時に元のパスのソケットファイルを消す)
- `sockat.ex` の `socket` も同じに渡せる

## ドキュメント

- [docs/MANUAL-ja.md](./docs/MANUAL-ja.md) — 使い方、経路の詳細、標準と違う点、エラー
- [docs/decisions/](./docs/decisions/INDEX.md) — 設計の判断 (DR)

## ライセンス

MIT License, Yoshiaki Kawazu (@kawaz)
