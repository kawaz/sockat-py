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
| `sockat.ex.bind(path)` など | 同じ + 何を失ったか・ソケットファイルの後始末・最終手段 | `Bound` / `Connected` ([後述](#後始末診断つきの呼び出し-sockatex)) |

- 戻り値は標準の `socket.socket` (長いパスでは、アドレスを元のパスで返す `socket.socket` の subclass)
- 標準が受け付ける長さのパスは標準にそのまま渡すので、標準と同じに振る舞う
- 長いパスでも、ソケットファイルは渡したパスにできる
- `path` は `str` / `bytes` / `os.PathLike`
- Python 3.11 以上。依存なし (純 Python)

## なぜ要るか

標準の `socket.bind` / `socket.connect` は `sun_path` に収まらないパスを `OSError('AF_UNIX path too long')` で断る。ファイルシステムには上限が無いのに、深い一時ディレクトリ (macOS の `$TMPDIR` はそれだけで 50 バイト前後、試験の枠組みやサンドボックスがさらに掘る) にソケットを置くとすぐに当たる。上限が効くのは `bind(2)` / `connect(2)` に渡す引数の長さなので、sockat は同じ場所を指す短い綴りでカーネルに渡す。

## 長いパスへの届き方

標準が受け付ける長さ (`sun_path` − 1) のパスは標準にそのまま渡す (経路 standard)。

| OS | 標準にそのまま渡す長さ |
|---|---|
| Linux / Android / illumos | 107 バイトまで |
| macOS / iOS / FreeBSD / NetBSD / OpenBSD | 103 バイトまで |

それを超えるパスは、OS ごとに次の順に経路を試し、最初に使えた経路で bind / connect する。経路は[仕様の「経路の名前」](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#経路の名前)で呼ぶ。

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` を使える) | thread-cwd → proc | proc-direct → thread-cwd → proc → proc-held → symlink-file |
| Linux / Android (`/proc` を使えない) | thread-cwd | relative → thread-cwd → symlink-file |
| FreeBSD | at | at-direct → at |
| macOS / iOS | symlink-file → thread-cwd | relative → widened → thread-cwd → symlink-file |
| NetBSD | symlink-file | relative → symlink-file |
| OpenBSD / illumos | なし (`ENAMETOOLONG`) | relative → symlink-file |
| その他の Unix | symlink-file (辿るかを実行時に見分ける) | relative → symlink-file |
| Windows | 対象外 (import で `ImportError`) | 同左 |

| 経路 | やり方 |
|---|---|
| D を通す経路 (thread-cwd、at、proc) | 自分専用のディレクトリ D を作って fd で掴み、その中にソケットのディレクトリを指す乱数の symlink R を作り、D を基準に `R/<ファイル名>` で渡して、R と D を消す。D は `TMPDIR`、`/tmp`、ソケットのディレクトリの順に、作れた所に作る |
| thread-cwd | D を通す経路。使い捨てのスレッドだけ cwd を D に移す (Linux / Android、macOS / iOS) |
| at | D を通す経路。`bindat(2)` / `connectat(2)` に D の fd を渡す (FreeBSD) |
| proc | D を通す経路。`/proc/self/fd/<D の fd>/R/<ファイル名>` で渡す (Linux / Android) |
| thread-cwd-sockdir、at-sockdir、proc-sockdir | bind だけ。D をどこにも作れない時の D 無しの形。ソケットのディレクトリに R (`.sockat.XXXXXXXXXX`) を作り、ソケットのディレクトリを基準に同じ手段で `.sockat.XXXXXXXXXX/<ファイル名>` を渡して、R を消す |
| proc-direct | connect だけ。`/proc/self/fd/<ソケットのディレクトリの fd>/<ファイル名>` で渡す |
| proc-held | connect だけ。D の中にソケットのファイルを指す symlink `s` を作り、`/proc/self/fd/<D の fd>/s` で渡す |
| relative | connect だけ。cwd からの相対パスで渡す。文字列を書き直すだけで cwd は変えない |
| widened | connect だけ。macOS のカーネルが受け付ける、253 バイトまで入る sockaddr で渡す |
| symlink-file | 他人が差し替えられない `TMPDIR` か `/tmp` に、ソケットのファイルを指す短い symlink を作って通し、すぐ消す。綴りにファイル名が出ない。bind は、bind が最後の要素の symlink を辿る OS (macOS と NetBSD で確認) でだけ通る。辿らないと確かめた OS (Linux / Android、FreeBSD、OpenBSD、illumos) では試さず、他の OS では辿らない時に次の経路に進む |
| symlink-dir | 最終手段を使う `sockat.ex.bind` だけ。置き場にソケットのディレクトリを指す短い symlink を作って通し、すぐ消す ([後述](#後始末診断つきの呼び出し-sockatex)) |
| at-direct | connect だけ。`connectat(2)` にソケットのディレクトリの fd とファイル名を渡す (FreeBSD) |
| fork-cwd、fork-cwd-sockdir、fork-cwd-direct | **無い** (仕様の fork の経路は作っていない。`sockat.ex.connect` の最終手段の fork-cwd-direct は `skipped` に入るだけ) |

- どの経路も呼び出し元のプロセスとスレッドの cwd を変えないので、マルチスレッドのプログラムから使える
- connect は、書き込み (D・R・symlink を作る) もスレッドも要らない経路 (proc-direct、relative、widened、at-direct) を先に試し、D を作る経路とスレッドを立てる経路をその後にする。D は 1 回の connect で 1 度だけ作り、D を使う経路 (thread-cwd、at、proc、proc-held) で共有する。どこにも作れなければ、D を使う経路はどれも飛ばす
- macOS と NetBSD の bind は symlink-file でファイル名の長さによらず届く (失うのは `respelled` だけ)
- D を通す経路の bind はディレクトリの深さによらず、ファイル名が 96 バイト (macOS / FreeBSD は 92、proc はおよそ 79) まで届く (`R/` の分、標準より短い)。D をどこにも作れない時は D 無しの形で 88 バイト (macOS / FreeBSD は 84、proc-sockdir はおよそ 72) まで
- OpenBSD / illumos と、D を通す経路の手段を使えない環境 (seccomp の filter の下で `/proc` の無い Linux 等) では、長いパスの `bind` は `ENAMETOOLONG` になる (仕様では fork-cwd の経路で届く所。Python には fork の経路が無い。ハブの D12-Q3 で kawaz 裁定)。そこでも届かせたい時は最終手段を使う `sockat.ex.bind` ([後述](#後始末診断つきの呼び出し-sockatex))
- どの経路にも収まらないパスは、標準で長すぎるパスと同じく `ENAMETOOLONG` (errno 付きの `OSError`) になる
- 各経路の条件と、どこから届かなくなるかは [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#長いパスへの届き方) にある

## 長いパスで標準と違う点

- listener (と accept したソケット) の `getsockname()` と、connect したソケットの `getpeername()` は元のパスを返す。カーネルが返すのは `R/<ファイル名>` (proc は `/proc/self/fd/<fd>/R/<ファイル名>`、D 無しの形は R が `.sockat.XXXXXXXXXX`、symlink-file は `<置き場>/sockat.XXXXXXXXXX`。R も symlink ももう無いので何にも当たらない) だが、sockat が返すソケットはそれを元のパスに差し替える
- 標準にそのまま渡せない長さのパスで、名前で終わらないもの (`dir/`、`dir/.`、`..` で終わるもの) は `ValueError` になる
- 先頭以外に NUL を含むパスは、長さによらず `ValueError` になる (標準は NUL で黙って切り詰める)。Linux で先頭が NUL のパス (abstract namespace) は標準にそのまま渡す
- sticky でなく他人も書けるソケットのディレクトリでは、D 無しの形の R を作ってから bind するまでの間に R を差し替えられ、別のディレクトリに bind しうる (同じ名前が既にあれば `EADDRINUSE` で、上書きはしない。D の中の R は D が 0700 なので差し替えられない)
- 途中でプロセスが死ぬと、置き場 (`TMPDIR`、`/tmp`、symlink-dir は `XDG_RUNTIME_DIR` も) に `sockat.XXXXXXXXXX`、ソケットのディレクトリに `.sockat.XXXXXXXXXX` が残ることがある
- その他 (ディレクトリを先に引くこと等) は [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#長いパスで標準と違う点)

## 後始末・診断つきの呼び出し (sockat.ex)

```python
import sockat.ex

listener, discard, losses, route, skipped = sockat.ex.bind(path)
...
discard()
listener.close()

sock, losses, route, skipped = sockat.ex.connect(path)
```

- `sockat.ex.bind` / `sockat.ex.connect` / `sockat.ex.connect_nonblocking` は、既定の `sockat.bind` / `sockat.connect` / `sockat.connect_nonblocking` と同じ経路を試し、通った経路の名前 (`route`)、試して使えなかった経路 (`skipped`、`name` と `reason`)、失ったもの (`losses`) を一緒に返す
- `sockat.ex.connect(...)` のようにモジュールを通して呼ぶ (`from sockat.ex import connect` と取り込むと既定の `sockat.connect` と紛れる)
- `sockat.ex` は `import sockat` では読み込まれない。使う人が `import sockat.ex` で選ぶ
- `discard()` は bind が作ったソケットファイルだけを消す (listener を開いている間に別のファイルに置き換わっていたら消さない)。何度呼んでもよく、listener は閉じない (閉じるのは利用者)。listener を閉じる前に呼ぶ (閉じた後に消して作り直された物は、ファイルシステムによっては見分けられず消しうる)
- `losses` は通った経路で失ったものの並び (`Loss` の `respelled`、`recreatable-spelling`、`cwd-relative`。何も失わなければ空)。分岐にはこれを使う。`route` と `skipped` は診断用で中身は約束しない
- `last_resort=True` で最終手段を使う: `sockat.ex.bind` は、既定の bind が `ENAMETOOLONG` になる所 (経路が全部使えない所) で最後に symlink-dir を試す (置き場にソケットのディレクトリを指す `sockat.XXXXXXXXXX` を作り、`<置き場>/sockat.XXXXXXXXXX/<ファイル名>` で bind して、すぐ消す。置き場は `XDG_RUNTIME_DIR`、`TMPDIR`、`/tmp` で、持ち主だけが書ける置き場を先に、無ければ共有の置き場。共有の置き場を通ると `recreatable-spelling` が付く)。connect の最終手段は fork の経路で、Python には無いので `skipped` に入るだけ
- 失敗した時は、例外 (型と `errno` は既定の呼び出しと同じ) に `skipped` と `route` の属性を付ける
- 詳細は [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#sockatex)

## 失うもの

`sockat.ex` の `losses` に入る名前と避け方。起きること・注意すること・避け方の推奨コードは [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#失うもの) にある (推奨コードは試験として動かしている)。

| 名前 | 起きること | 避け方 |
|---|---|---|
| [respelled](./docs/MANUAL-ja.md#respelled) | 長いパスで、カーネルが返すアドレスが元のパスでない | 元のパス (`os.unlink(path)`) か `discard()` で消す |
| [cwd-relative](./docs/MANUAL-ja.md#cwd-relative) | 相対パスで bind すると、カーネルが cwd からの相対の綴りを覚える | bind の前に `os.path.abspath(path)` で絶対パスにする |
| [forked](./docs/MANUAL-ja.md#forked) | sockat-py には fork の経路が無いので付かない | — |
| [recreatable-spelling](./docs/MANUAL-ja.md#recreatable-spelling) | 最終手段の symlink-dir が共有の置き場を通り、綴りを他の利用者が作り直せる | 元のパスか `discard()` で消す。持ち主だけが書ける置き場 (`$XDG_RUNTIME_DIR` 等) を用意する |

## 使ってはいけない書き方

無関係なファイルを消したり、ファイルを消し損ねたりする書き方。詳細と代わりの書き方のコードは [docs/MANUAL-ja.md](./docs/MANUAL-ja.md#使ってはいけない書き方) にある。

| 書き方 | 代わりに |
|---|---|
| `os.unlink(socket.socket.getsockname(listener))` (カーネルが返す綴りで消す) | `os.unlink(path)` (元のパス) か `discard()` |
| `sockat.bind("server.sock")` → `os.chdir(...)` → `os.unlink("server.sock")` | bind の前に `os.path.abspath(path)`。`discard()` |
| `listener.close()` だけで後始末を終える | `discard()` (listener を閉じる前に) か `os.unlink(path)` |
| `discard()` だけを呼び listener を閉じない | listener も `close()` する |
| `if route == "symlink-dir":` (`route`・`skipped`・`reason` で分岐する) | `if sockat.ex.Loss.RECREATABLE_SPELLING in losses:` (`losses` で分岐する) |

## asyncio

```python
listener = sockat.bind(path)
server = await asyncio.start_unix_server(handle, sock=listener)

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- 引数を足さずに短いパスと同じに動く (Python 3.13 以降は server を閉じる時に元のパスのソケットファイルを消す)
- `sockat.ex` の `listener` / `sock` も同じに渡せる

## ドキュメント

- [docs/MANUAL-ja.md](./docs/MANUAL-ja.md) — 使い方、経路の詳細、標準と違う点、エラー
- [docs/decisions/](./docs/decisions/INDEX.md) — 設計の判断 (DR)

## ライセンス

MIT License, Yoshiaki Kawazu (@kawaz)
