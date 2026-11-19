# sockat マニュアル

> [English](./MANUAL.md) | 日本語

sockat-py の使い方と、長いパスにどう届くか、長いパスで標準と違う点のリファレンス。言語によらない振る舞いの正本は [sockat の仕様](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)、Python 版の設計の判断は [DR-0001](./decisions/DR-0001-python-port-design.md)、[DR-0004](./decisions/DR-0004-own-directory-route.md)、[DR-0005](./decisions/DR-0005-bind-any.md) にある。

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
| `sockat.bind(path)` | `socket()` → `bind(path)` → `listen()` | listen 済みの `socket.socket` (ブロッキング) |
| `sockat.connect(path)` | `socket()` → `connect(path)` | `socket.socket` (ブロッキング) |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | 非ブロッキングの `socket.socket` |
| `sockat.ex.bind(path)` など | 同じ + 何を失ったか・ソケットファイルの後始末・最終手段 | `Bound` / `Connected` ([後述](#sockatex)) |

- 標準が受け付ける長さのパスでは、戻り値は素の `socket.socket`。長いパスでは、アドレスを元のパスで返す `socket.socket` の subclass ([後述](#アドレスは元のパスを返す))
- `path` は `str` / `bytes` / `os.PathLike` (標準の `bind` / `connect` は `os.PathLike` を受けないが、sockat は受ける)
- 標準が受け付ける長さ ([OS ごとの値](#長いパスへの届き方)) のパスは標準をそのまま呼ぶ
- どの経路のソケットも `socket.socket(AF_UNIX, SOCK_STREAM)` で作るので、標準と同じ設定になる (close-on-exec、`socket.setdefaulttimeout` の値、listen の既定の backlog)
- ソケットファイルは渡したパスにできる。ソケットを閉じても消えない (標準と同じ)。消す時は `sockat.ex.bind` の `discard()` か、元のパスで `os.unlink(path)` (unlink には `sun_path` の上限が無い)
- backlog を変えたい時は、返った listener で `listen(n)` し直す

## connect と connect_nonblocking

- **`connect`** はブロックする。標準の `connect` と同じく `settimeout` の値に従う
- **`connect_nonblocking`** はイベントループで使う。ソケットを connect の前に非ブロッキングにし、そのまま返す。相手の backlog が満杯なら待たずに失敗する (Linux は `BlockingIOError`、macOS は `ConnectionRefusedError`)。Unix domain の connect は呼び出しの中で終わるか失敗するので、後で待つものは無い

## sockat.ex

後始末・診断つきの呼び出し ([DR-0006](./decisions/DR-0006-sockat-ex.md)、ハブの [DR-0015](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0015-ex-with-last-resort-and-diagnostics.md))。`import sockat` では読み込まれない。使う人が `import sockat.ex` で選ぶ。

```python
import sockat.ex

listener, discard, losses, route, skipped = sockat.ex.bind(path)
try:
    ...
finally:
    listener.close()
    discard()

sock, losses, route, skipped = sockat.ex.connect(path)
```

| 関数 | 既定の呼び出し | 戻り値 |
|---|---|---|
| `sockat.ex.bind(path, *, last_resort=False)` | `sockat.bind(path)` | `Bound` (`listener`、`discard`、`losses`、`route`、`skipped`) |
| `sockat.ex.connect(path, *, last_resort=False)` | `sockat.connect(path)` | `Connected` (`sock`、`losses`、`route`、`skipped`) |
| `sockat.ex.connect_nonblocking(path, *, last_resort=False)` | `sockat.connect_nonblocking(path)` | `Connected` |

- `sockat.ex.connect(...)` のようにモジュールを通して呼ぶ。`from sockat.ex import connect` と取り込むと、既定の `sockat.connect` と紛れる
- `last_resort` を渡さない時は、既定の呼び出しと同じ経路を同じ順に試し、同じ所に届く。`listener` / `sock` も既定の呼び出しが返すものと同じ
- 戻り値は `NamedTuple` で、名前でも位置でも取り出せる

### discard

`sockat.ex.bind` が返す、bind が作ったソケットファイルだけを消す関数。

- bind が通った直後 (listen の前) に、ソケットファイルの realpath と `(st_dev, st_ino)` を覚える。覚えられなければ listener を閉じて例外を返す (ファイルは消さない)
- 相対パスの時は、パスを引く前と bind の後でプロセスの cwd の `(st_dev, st_ino)` を比べ、違えば覚えられない時と同じに扱う (errno の無い `OSError`)。他のスレッドが間に cwd を変えると、bind と覚えるのが別の cwd の下になり、`discard()` が同じ名前の無関係なファイルを消しうるため。変えて戻した場合は見えない
- `discard()` は、その realpath のファイルがまだ同じファイルの時だけ消す。無い時、別のファイルに置き換わっている時は何もしない
- 確かめてから消すまでの間の置き換えは防げない (POSIX に inode を指定して消す手段が無い)
- `discard()` は何度呼んでもよい。listener は閉じない (閉じるのは利用者。asyncio に渡したら asyncio)
- ファイルが無い以外のエラー (ディレクトリの `EACCES` 等) は `OSError`
- 短いパスでも同じに動く。Linux の abstract のアドレスでは何もしない

### last_resort

`last_resort=True` は、既定の経路が全部使えない所で、何かを失ってでも届かせる最終手段を使う。引数を渡す手間が、失うものを受け入れる意思表示になる。

| 関数 | 最終手段 |
|---|---|
| `sockat.ex.bind` | 既定の bind が `ENAMETOOLONG` になる所 (経路が全部使えない所) で、最後に [symlink-dir](#symlink-dir) を試す。共有の置き場を通ると `recreatable-spelling` を失う |
| `sockat.ex.connect`、`sockat.ex.connect_nonblocking` | 仕様の最終手段は fork-cwd-direct (fork した子がファイル名で connect する)。sockat-py には fork の経路が無いので、`skipped` に `no fork route in sockat-py` の理由で入れて `ENAMETOOLONG` を返す。届く所は既定の connect と同じ |

### 何を失ったか

| 項目 | 中身 |
|---|---|
| `losses` | 通った経路で失ったものの並び (`tuple[Loss, ...]`)。何も失っていなければ空。分岐にはこれを使う |
| `route` | 通った経路の名前 (`str`)。[仕様の「経路の名前」](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#経路の名前) (`"standard"`、`"symlink-file"`、`"thread-cwd"`、`"symlink-dir"` 等) |
| `skipped` | `route` より前に試して使えなかった経路の並び (試した順)。各要素は `Skipped(name, reason)` で、`name` は経路の名前、`reason` は使えなかった理由の短い英語 (`"path too long"`、`"seccomp filter"`、`"/proc unusable"`、`"no place for D"` 等) |

```python
if sockat.ex.Loss.RECREATABLE_SPELLING in losses:
    ...  # ソケットファイルは元のパスで消す (discard 等)
```

`Loss` は `enum.StrEnum` で、文字列にすると (`str()`、`.value`) [仕様の「失うもの」](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#失うもの)の名前になる。

| `Loss` | 名前 | 失うもの | 付く時 |
|---|---|---|---|
| `RESPELLED` | [`respelled`](#respelled) | カーネルが返すアドレスが元のパスでない。綴りは他の利用者に見えうる (Linux の `/proc/net/unix`) ので、プロセスの cwd が他人も書ける場所だと、相手が作り直した後にその綴りで消すと相手の選んだディレクトリの同じ名前のファイルを消しうる。元のパスで消せば当たらない | standard 以外の経路を通った |
| `RECREATABLE_SPELLING` | [`recreatable-spelling`](#recreatable-spelling) | カーネルが返す綴りが共有の置き場 (`/tmp` 等) を通る。他の利用者が綴りを見て作り直した後にその綴りで消すと、相手の選んだディレクトリの同じ名前のファイルを消しうる。元のパスで消せば当たらない | symlink-dir が共有の置き場を通った |
| `CWD_RELATIVE` | [`cwd-relative`](#cwd-relative) | カーネルが覚えた綴りが cwd からの相対パス。cwd を変えた後にその綴りで消す・引くと別の場所に当たる (標準が元から持つ危険)。元のパスを絶対パスにして消せば当たらない | 相対パスで standard を通った (abstract のアドレスには付かない) |
| `FORKED` | [`forked`](#forked) | fork の副作用 | sockat-py には fork の経路が無いので付かない |

- 名前の間に大小や包含の関係は無い。名前は約束で、変えずに足すだけ。知らない名前は「何かを失った」とだけ読む
- `route`・`skipped` は診断用で、中身を約束しない (経路は増えたり変わったりする)
- `route` より前に試した経路は、どれも使えなかったもの (実際に bind / connect して失敗したら、そこで例外を返して次を試さない)
- 仕様の fork の経路 (bind の `fork-cwd`、`fork-cwd-sockdir`、最終手段を使う connect の `fork-cwd-direct`) は、試す順の位置に「sockat-py に無い」理由で `skipped` に入る
- connect の綴りは相手に残らないので、connect の `losses` は空 (失うものが付くのは fork-cwd-direct の `forked` だけで、sockat-py には無い)

| `sockat.ex.bind` の例 (macOS) | `losses` | `route` | `skipped` の `name` |
|---|---|---|---|
| 短い絶対パス | (空) | `standard` | (無し) |
| 短い相対パス | `cwd-relative` | `standard` | (無し) |
| 長いパス | `respelled` | `symlink-file` | `standard` |
| 長いパスで、symlink-file の置き場が無い | `respelled` | `thread-cwd` | `standard`、`symlink-file` |
| 同、D もどこにも作れない | `respelled` | `thread-cwd-sockdir` | `standard`、`symlink-file`、`thread-cwd`、`fork-cwd` |
| 同、スレッド cwd も使えず、symlink-dir に持ち主だけが書ける置き場 (`XDG_RUNTIME_DIR` 等) がある | `respelled` | `symlink-dir` | `standard`、`symlink-file`、`thread-cwd`、`fork-cwd` |
| 同、共有の置き場だけがある | `respelled`、`recreatable-spelling` | `symlink-dir` | `standard`、`symlink-file`、`thread-cwd`、`fork-cwd` |

失敗した時は既定の呼び出しと同じ例外 (型と `errno` はそのまま) に、次の 2 つの属性を付けて送出する。

| 属性 | 中身 |
|---|---|
| `skipped` | 失敗するまでに試して使えなかった経路の並び (`skipped` と同じ形) |
| `route` | 実際に bind / connect して失敗した経路の名前。そこまで進まなかった時 (ディレクトリが無い、どの経路も使えない `ENAMETOOLONG`、引数の誤り) は `None` |

```python
try:
    sock, losses, route, skipped = sockat.ex.connect(path)
except OSError as e:
    print(e.errno, getattr(e, "route", None), getattr(e, "skipped", ()))
```

### symlink-dir

`sockat.ex.bind(path, last_resort=True)` の最終手段。

1. 置き場の候補を集める: `XDG_RUNTIME_DIR`、`TMPDIR` (それぞれ空でない絶対パスの時)、`/tmp` の順。`/tmp` か前の候補と区切り単位で同じ綴りは飛ばす
2. 候補を symlink を辿って `stat` し、持ち主だけが書ける置き場 (ディレクトリ、持ち主が自分、`mode & 0o022 == 0`) を先に、共有の置き場 (ディレクトリ、sticky、持ち主が root か自分) を後に並べる。どちらでもない候補は使わない
3. 順に、綴り `<置き場>/sockat.XXXXXXXXXX/<ファイル名>` が `sun_path` に収まるかを確かめ、収まれば置き場にソケットのディレクトリの realpath を指す `sockat.XXXXXXXXXX` (乱数 10 文字、`EEXIST` で引き直し 16 回で打ち切り) を作る。収まらない・作れない候補には何も残さずに次へ進む
4. その綴りで bind し、成功でも失敗でも symlink を消す。bind が失敗したら (`EADDRINUSE` 等) その例外を返し、次の置き場を試さない
5. どの候補でも届かなければ `ENAMETOOLONG`

- 持ち主だけが書ける置き場を通った時、綴りを作り直せるのは自分だけなので、symlink を消した後のカーネルの綴りは何にも当たらない
- 共有の置き場を通った時、symlink を消した後に他の利用者が同じ綴りを作り直せる (Linux の `/proc/net/unix` 等で綴りが見える)。カーネルの綴りでファイルを消す処理は、相手の選んだディレクトリの同じ名前のファイルを消しうる。sockat の listener は元のパスを返すので、asyncio の後始末は元のパスで消す
- 届くファイル名は `sun_path` − 1 − (置き場の綴りの長さ + 19) バイトまで (`/tmp` なら Linux 84、macOS / BSD 80)

## 失うもの

経路を通ったことで、標準と比べて何を失うか ([仕様の「失うもの」](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#失うもの))。`sockat.ex` の `losses` はこの名前の並びで返す ([何を失ったか](#何を失ったか))。下の避け方のコードは試験 (`tests/test_docs.py`) がこの文書から読み出して動かす。`path` はソケットのパス。

### respelled

- 起きること: カーネルが返すアドレス (`socket.socket.getsockname(listener)` のように標準の実装を呼ぶと見える) が元のパスでない綴りになる ([長いパスで標準と違う点](#長いパスで標準と違う点))。sockat が返すソケットの `getsockname()` / `getpeername()` は元のパスを返す
- 注意すること: カーネルが返す綴りでファイルを unlink / stat しない。綴りの中の乱数の名前 R は消してあるので普段は何にも当たらないが、綴りは他の利用者に見えうる (Linux の `/proc/net/unix`)。プロセスの cwd が他人も書ける場所 (`/tmp` 等) だと、相手が見て作り直した後にその綴りで unlink すると、相手の選んだディレクトリの同じ名前のファイルを消しうる
- 避け方: 元のパスを持っておき、消す時はそれを使う。`sockat.ex.bind` の `discard()` を使う

```python
import os

import sockat
import sockat.ex

listener = sockat.bind(path)
...
listener.close()
os.unlink(path)

listener, discard, losses, route, skipped = sockat.ex.bind(path)
...
listener.close()
discard()
```

### cwd-relative

- 起きること: 渡した相対パスがそのまま標準に渡り、カーネルが相対の綴りを覚える
- 注意すること: cwd を変えた後に、その綴り (や渡した相対パス) で unlink / stat すると別の場所に当たる。sockat が経路で失わせたものでなく標準が元から持つ危険だが、知らせる
- 避け方: bind の前にパスを絶対パスにする。消す時も絶対パスか `discard()` を使う

```python
import os

import sockat

path = os.path.abspath(path)
listener = sockat.bind(path)
...
listener.close()
os.unlink(path)
```

### forked

sockat-py には fork の経路が無いので付かない。

### recreatable-spelling

- 起きること: 最終手段を使う `sockat.ex.bind` が共有の置き場 (`/tmp` 等) の [symlink-dir](#symlink-dir) を通り、カーネルが返す綴りが `<置き場>/sockat.XXXXXXXXXX/<ファイル名>` になる
- 注意すること: 名前は乱数なので事前には当てられないが、bind の後は他の利用者に見えうる (Linux の `/proc/net/unix`)。他の利用者がそれを作り直した後にその綴りで unlink すると、相手の選んだディレクトリの同じ名前のファイルを消しうる。名前の長さは防御でない (名前は bind の後に見える)
- 避け方: カーネルが返す綴りで消さず、元のパスか `discard()` で消す。持ち主だけが書ける置き場 (`$XDG_RUNTIME_DIR` や、持ち主だけが書ける `$TMPDIR`) を用意すれば、そちらが先に使われてこの名前は付かない

```python
import sockat.ex

listener, discard, losses, route, skipped = sockat.ex.bind(path, last_resort=True)
...
listener.close()
discard()
```

## 長いパスへの届き方

標準が受け付ける長さ (`sun_path` − 1) のパスは標準にそのまま渡す (経路 standard)。

| OS | 標準にそのまま渡す長さ |
|---|---|
| Linux / Android / illumos | 107 バイトまで |
| macOS / iOS / FreeBSD / NetBSD / OpenBSD | 103 バイトまで |

それを超えるパスは、まずソケットのディレクトリを引き (無ければ `ENOENT`)、OS ごとに次の順に経路を試し、最初に使えた経路で bind / connect する。どの経路も呼び出し元のプロセスの cwd も呼び出し元のスレッドの cwd も変えない。経路は[仕様の「経路の名前」](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#経路の名前)で呼ぶ。

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` を使える) | [thread-cwd](#d-を通す経路) → [proc](#d-を通す経路) | [proc-direct](#connect-だけの経路) → [thread-cwd](#d-を通す経路) → [proc](#d-を通す経路) → [proc-held](#connect-だけの経路) → [symlink-file](#symlink-file) |
| Linux / Android (`/proc` を使えない) | [thread-cwd](#d-を通す経路) | [relative](#connect-だけの経路) → [thread-cwd](#d-を通す経路) → [symlink-file](#symlink-file) |
| FreeBSD | [symlink-file](#symlink-file) → [at](#d-を通す経路) | [at-direct](#connect-だけの経路) → [at](#d-を通す経路) |
| macOS / iOS | [symlink-file](#symlink-file) → [thread-cwd](#d-を通す経路) | [relative](#connect-だけの経路) → [widened](#connect-だけの経路) → [thread-cwd](#d-を通す経路) → [symlink-file](#symlink-file) |
| NetBSD / OpenBSD / illumos / その他の Unix | [symlink-file](#symlink-file) | [relative](#connect-だけの経路) → [symlink-file](#symlink-file) |

- bind は、Linux / Android 以外で standard の次に [symlink-file](#symlink-file) を試す。bind が symlink を辿る OS (macOS で確認) では、ファイル名の長さによらず届く。辿らない時は次の経路に進む
- connect は、書き込み (D・R・symlink を作る) もスレッドも要らない経路 (proc-direct、relative、widened、at-direct) を先に、D を作る経路とスレッドを立てる経路をその後に試す。書き込みもスレッドも要らない経路は速く、relative や widened は D を通す経路より遠くまで届く場面がある
- D は 1 回の呼び出しで 1 度だけ作り、D を使う経路 (thread-cwd、at、proc、proc-held) で共有する。どこにも作れなかったら、D を使う経路はどれも使えないとして飛ばし、作り直さない
- D をどこにも作れない時、bind は同じ手段を [D 無しの形](#d-無しの形) (thread-cwd-sockdir、at-sockdir、proc-sockdir) で試す
- 仕様の fork の経路 (fork-cwd、fork-cwd-sockdir、fork-cwd-direct) は作っていない。Python から fork した子は Python を実行するので、仕様が求める「子の区間は async-signal-safe」を満たせないため ([DR-0001](./decisions/DR-0001-python-port-design.md))。仕様で bind が fork-cwd に進む所は `ENAMETOOLONG` になる (ハブの D12-Q3 で kawaz 裁定)。そこでも届かせたい時は [`sockat.ex.bind` の最終手段](#last_resort)
- 順番の最後まで届かなかったパスは `ENAMETOOLONG` になる

### D を通す経路

thread-cwd・at・proc。

1. 置き場に自分専用のディレクトリ D (mode 0700) を作り、fd で開いて掴む
2. D が自分の物 (ディレクトリ、持ち主が自分、group と other に権限が無い) か `fstat` で確かめ、0700 にそろえる
3. D の中に、ソケットのディレクトリの realpath を指す乱数 10 文字の symlink R を作る
4. D の fd を基準に `R/<ファイル名>` で bind / connect する
5. 呼び出しの最後に、成功でも失敗でも R と D を消す (D は 1 回の呼び出しで 1 度だけ作り、D を使う経路で共有する)

- ディレクトリの深さによらず届く。綴り `R/<ファイル名>` に収まるファイル名は Linux 96、Apple の OS / FreeBSD 92 バイトまで
- 確かめに落ちた D は他人の物とみなし、中身も含めて触らない
- 作った直後の D を開けない時 (umask が持ち主の権限を削った時。macOS は検索権限、`O_RDONLY` で開く OS は読み取り権限) は、名前で `stat` (symlink を辿らない) して手順 2 と同じ条件なら `rmdir` で消し、その置き場では作れないとして次へ進む。名前で確かめてから消すまでにすり替えられても、`rmdir` は空のディレクトリしか消さない (ハブの D12-Q5)
- 長さと手段を使えるかは D を作る前に確かめる。使えなければ何も作らずに次へ進む
- proc の綴りの長さは D の fd の番号で変わるので、D を作る前は見積もりで確かめ、開いた後に確かめ直す。収まらなければ proc を使えないとして次へ進む。D は後の経路と共有し、片付けは呼び出しの最後に 1 回

#### D の置き場

| 順 | 置き場 | D の名前 |
|---|---|---|
| 1 | `${TMPDIR:-/tmp}` (相対パスも可) | `sockat.XXXXXXXXXX` |
| 2 | `/tmp` (1 で作れない時だけ。1 と同じなら飛ばす) | `sockat.XXXXXXXXXX` |
| 3 | ソケットのディレクトリ (2 でも作れない時だけ) | `.sockat.XXXXXXXXXX` |

- 置き場の安全 (sticky 等) と長さは問わない。D を fd で掴み、綴りに置き場が出ないため
- D の開き方は、Linux が `O_PATH`、macOS が `O_SEARCH` (umask が持ち主の読み取りを削っても開ける)、他は `O_RDONLY`
- どこにも作れない時、bind は [D 無しの形](#d-無しの形)に進み、connect は D を使う経路を飛ばして順の次の経路に進む

#### 手段

| 経路 | OS | やり方 | 使えない時 |
|---|---|---|---|
| thread-cwd | Linux / Android | 使い捨ての `threading.Thread` で ctypes の `unshare(CLONE_FS)` → `os.fchdir(D)` → `R/<ファイル名>` | seccomp の filter が掛かっている (docker の既定、systemd の `SystemCallFilter=` 等。filter が `unshare` でプロセスを kill しうるので呼ばない)、`unshare` を断られた、スレッドを作れない |
| thread-cwd | macOS / iOS | 使い捨ての `threading.Thread` で ctypes の `pthread_fchdir_np(D)` (libSystem の非公開 API) → `R/<ファイル名>` | 関数が無い・失敗した、スレッドを作れない |
| at | FreeBSD | ctypes の `bindat(2)` / `connectat(2)` に D の fd と `R/<ファイル名>` | ファイル名 > 92 |
| proc | Linux / Android | `/proc/self/fd/<D の fd>/R/<ファイル名>` を標準に渡す | `/proc/self/fd/<fd>` を引けない (`/proc` の無い chroot や最小のコンテナ)、綴りが 107 バイトを超える (ファイル名およそ 79 まで) |

- thread-cwd では、プロセスの cwd も他のスレッドの cwd も変わらない。connect した相手から見える peer の pid も呼び出し元のまま
- Linux で thread-cwd が使えない時は、同じ D で proc を試す

#### D 無しの形

bind だけが、D をどこにも作れない時に試す。経路は thread-cwd-sockdir (FreeBSD は at-sockdir)、proc-sockdir の順。

1. ソケットのディレクトリの中に、ソケットのディレクトリの realpath を指す symlink R を、ソケットのディレクトリの fd を基準に作る。名前は `.sockat.` + 乱数 10 文字
2. ソケットのディレクトリの fd を基準に、[手段](#手段)を同じ順に使って `.sockat.XXXXXXXXXX/<ファイル名>` で bind する。thread-cwd-sockdir は使い捨てのスレッドの cwd をソケットのディレクトリに移し、proc-sockdir は `/proc/self/fd/<ソケットのディレクトリの fd>/.sockat.XXXXXXXXXX/<ファイル名>` を渡す
3. 成功でも失敗でも R を消す

- 綴りは 19 バイト + ファイル名。ファイル名は Linux 88、macOS / FreeBSD 84 バイトまで (proc-sockdir はおよそ 72)
- ソケットのディレクトリを開けない、手段を使えない、綴りが収まらない時は、何も作らずに `ENAMETOOLONG`。R を作れない時 (名前の衝突が 16 回続いた等) も `ENAMETOOLONG`
- D を作れて手段が全部使えなかった時は、この形を試さずに `ENAMETOOLONG`

### connect だけの経路

connect 系だけが使う経路。OS ごとの順 ([長いパスへの届き方](#長いパスへの届き方)の表) で、D を通す経路と並べて試す。

| 経路 | やり方 | 使える条件 |
|---|---|---|
| proc-direct | ソケットのディレクトリの fd で `/proc/self/fd/<fd>/<ファイル名>` を標準に渡す | Linux / Android。ソケットのディレクトリを開け、綴りが 107 バイト以内 (ファイル名およそ 90 まで) |
| proc-held | D を通す経路と共有する D (まだ無ければ作る) の中に、ソケットのファイルを指す symlink `s` を作って `/proc/self/fd/<D の fd>/s` で connect し、消す | Linux / Android。D をどこかに作れて、`s` を作れる ([DR-0002](./decisions/DR-0002-unsafe-base-held-by-fd.md)) |
| relative | cwd からの相対パスを標準に渡す。文字列を書き直すだけで `chdir` しない | 元のパスが絶対パスで、相対パスが標準にそのまま渡せる長さ |
| widened | ctypes で libc の `connect(2)` に、`sun_path` を超えて 253 バイトまで入る sockaddr で元のパス (無理なら相対パス) を渡す | macOS / iOS。パスが 253 バイト以内で、カーネルが受け付ける |
| at-direct | ctypes の `connectat(2)` にソケットのディレクトリの fd とファイル名 | FreeBSD。ソケットのディレクトリを開け、ファイル名が 103 バイト以内 |

- relative: パスが cwd (`getcwd`、または cwd と同じディレクトリを指す `$PWD`) の下にあれば前を削る。そうでなければ両方を実パスに揃えて `..` で上る綴りを作る。短い方を使う
- relative: 元と同じディレクトリを指すことを確かめてから使い、connect の後に cwd が変わっていないことも確かめる ([後述](#relative-は他のスレッドの-chdir-に弱い))
- `bind` は proc-direct・relative・widened・symlink-dir を使わない。綴りが後で無関係なファイルに当たるか (proc-direct は閉じた fd の番号の使い回し、relative は cwd の変化)、listener のアドレスが途中で切れて標準の `getsockname` が読み過ぎるか (widened)、D を通す経路で足りる (symlink-dir) ため ([DR-0004](./decisions/DR-0004-own-directory-route.md))。symlink-dir は最終手段を使う `sockat.ex.bind` だけが最後に使う ([前述](#symlink-dir))

### symlink-file

bind (Linux / Android 以外) と connect が使う。

1. 置き場の候補 `TMPDIR` (絶対パスで `/tmp` と違う時)、`/tmp` を順に見て、綴り `<置き場>/sockat.XXXXXXXXXX` が `sun_path` に収まり (connect は macOS で広げた sockaddr の 253 バイトまで)、安全な置き場である最初の候補に、ソケットのファイルのパス (相対パスなら cwd を前に付ける) を指す `sockat.XXXXXXXXXX` (乱数 10 文字、`EEXIST` で引き直し 16 回で打ち切り) を作る。収まらない・安全でない・作れない候補には何も作らず次へ進む
2. その綴りで bind / connect し、成功でも失敗でも symlink を消す

- **安全な置き場**はディレクトリで、次のどちらか
  - sticky bit が立っていて、持ち主が root か自分 (`/tmp` がこれ)
  - 持ち主が自分で、group と other に書き込み権限が無い (macOS の `$TMPDIR` がこれ)
- 綴りにファイル名もディレクトリも出ないので、ファイル名の長さによらず届く
- bind は、bind が最後の要素の symlink を辿る OS でだけ通る (macOS で確認。Linux / Android は辿らないので試さない。他の OS は未確認)。bind が失敗した時は元のパスを `lstat` し、物があればそのエラー (`EADDRINUSE` 等) を返して次を試さない。無いか確かめられなければ、カーネルが辿らなかったとみなしてこの経路を使えないとして次の経路へ進む (ハブの [DR-0016](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0016-bind-through-file-symlink.md))
- bind で失うのは `respelled` だけ。カーネルが返すアドレスは symlink の綴りで、その綴りで unlink しても置き場の中の項目が消えるだけ (symlink はもう無い) なので、無関係なファイルを消させない

### 経路の失敗の扱い

経路が実際に bind / connect して失敗した時 (`ENOENT`、`EADDRINUSE`、`ECONNREFUSED` 等) は、そのエラーを返して次の経路も次の置き場も試さない。次へ進むのは、その経路自体が使えない時 (綴りが収まらない、D や R を作れない、手段が使えない、`/proc` を使えない、カーネルが広げた sockaddr を受け付けない、symlink の置き場が安全でない・symlink を作れない、symlink-file の bind が失敗して元のパスに物が無い) だけ。

### どこから届かなくなるか

標準にそのまま渡せない長さのパスが `ENAMETOOLONG` になる時 (ディレクトリがあり、D を作れる前提。数字はバイト)。

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` を使える) | ファイル名 > 96 (thread-cwd を使えなければおよそ 79) | 届く (proc-held) |
| Linux / Android (`/proc` を使えない) | ファイル名 > 96、または thread-cwd を使えない | relative > 107、thread-cwd が使えず (ファイル名 > 96 か thread-cwd を使えない)、symlink-file も使えない |
| FreeBSD | ファイル名 > 92 (symlink-file を数えない) | ファイル名 > 103 |
| macOS / iOS | macOS: 届く (symlink-file)。symlink-file が使えない時はファイル名 > 92、または thread-cwd を使えない<br>他の Apple の OS: ファイル名 > 92、または thread-cwd を使えない | 元のパスも相対パスも > 253、thread-cwd が使えず (ファイル名 > 92 か thread-cwd を使えない)、symlink-file も使えない |
| NetBSD / OpenBSD | 長いパス全部 (symlink-file を数えない) | relative > 103、symlink-file も使えない |
| illumos | 長いパス全部 (symlink-file を数えない) | relative > 107、symlink-file も使えない |

- D をどこにも作れない時、bind は [D 無しの形](#d-無しの形)でファイル名 88 (macOS / FreeBSD 84、proc-sockdir だけならおよそ 72) まで届く (ソケットのディレクトリを開けて R を作れる時。手段を使えない環境は上の表と同じく届かない)。connect は順のうち D を使わない経路 (proc-direct、relative、widened、at-direct、symlink-file) の分だけ届く (proc-held も使えない)
- bind の欄で symlink-file を数えているのは macOS だけ。Linux / Android は bind が symlink を辿らないので試さない。他の OS は辿るかが未確認
- 最終手段を使う `sockat.ex.bind` は、`bind` が `ENAMETOOLONG` になる所でも、[symlink-dir](#symlink-dir) の綴り `<置き場>/sockat.XXXXXXXXXX/<ファイル名>` が `sun_path` に収まり、使える置き場があれば届く
- symlink-file と symlink-dir を使えないのは、symlink を作れる安全な置き場が無い時 (`TMPDIR` も `/tmp` も使えないサンドボックス等)
- パス全体が `PATH_MAX` (macOS 1024、Linux 4096 バイト) を超えるパスは、どの経路でも `ENAMETOOLONG` になる (他のシステムコールでも開けないパスで、sockat の対象外)

## 長いパスで標準と違う点

### アドレスは元のパスを返す

長いパスでは、カーネルが返すアドレス (`getsockname(2)` / `getpeername(2)`) は元のパスでなく、bind の時にカーネルに渡した綴りになる。sockat が返すソケットは、それを元のパスに差し替える。

| ソケット | 元のパスを返すもの | それ以外 |
|---|---|---|
| `bind` / `sockat.ex.bind` の listener | `getsockname()` | 標準のまま |
| listener の `accept()` が返すソケット | `getsockname()` | `getpeername()` は標準のまま (相手の綴り。たいてい空) |
| `connect` / `connect_nonblocking` のソケット | `getpeername()` | `getsockname()` は標準のまま (たいてい空) |

- 元のパスは `os.fsdecode` した `str` (標準がパスのアドレスを返すのと同じ型)。相対パスで渡したら相対パスのまま
- `dup()` したソケットも同じに返す。`socket.fromfd` 等で fd から作り直したソケットは標準の型なので、カーネルの綴りを返す
- これで、アドレスを読み返す上位 (asyncio の後始末、`repr`、ログ) が元のパスで動く ([DR-0003](./decisions/DR-0003-report-the-given-path.md))

カーネルが返す綴りは次の通り (`socket.socket.getsockname(s)` のように標準の実装を呼ぶと見える)。R と symlink は bind の後に消えているので、この綴りを後で解決しても普段は何にも当たらない。ただし綴りは他の利用者に見えうる (Linux の `/proc/net/unix`) ので、プロセスの cwd が他人も書ける場所の時と、共有の置き場の symlink-dir を通った時は、他の利用者が綴りを作り直せる ([`sockat.ex` の `losses`](#何を失ったか))。ソケットファイルは綴りでなく元のパスで消す。R を乱数の名前にするのは、たまたまある同じ名前の物に当たる事故を防ぐためで、他の利用者への防御ではない。

| bind の経路 | 返るもの |
|---|---|
| thread-cwd、at | `R/<ファイル名>` |
| proc | `/proc/self/fd/<fd>/R/<ファイル名>` |
| thread-cwd-sockdir、at-sockdir | `.sockat.XXXXXXXXXX/<ファイル名>` |
| proc-sockdir | `/proc/self/fd/<fd>/.sockat.XXXXXXXXXX/<ファイル名>` |
| symlink-file | `<置き場>/sockat.XXXXXXXXXX`。その綴りで unlink しても置き場の中の項目が消えるだけで、無関係なファイルには当たらない |
| symlink-dir (`sockat.ex.bind` の最終手段) | `<置き場>/sockat.XXXXXXXXXX/<ファイル名>`。共有の置き場では他の利用者が作り直せる |

他のプログラムが広げた sockaddr で bind した listener (macOS の Node.js 等) に connect した時の `getpeername()` は途中で切れたパスで、macOS の CPython では切れた後ろに無関係なバイトが付くことがある (標準の `getpeername` の振る舞い。取得で例外にはならない。[原因](./findings/2026-11-18-cpython-getsockname-widened-sockaddr.md))。

### 名前で終わらないパスは ValueError

標準にそのまま渡せない長さのパスで、最後がファイル名でないもの (`dir/`、`dir/.`、`dir/..`、`..`) は、何も作らずに `ValueError` を返す。それより短いパスは標準にそのまま渡すので、カーネルのエラーになる。

### NUL を含むパスは ValueError

先頭以外に NUL を含むパスは、長さによらず `ValueError` を返す。標準は NUL で黙って切り詰めて別のパスに bind する (`b"/tmp/a\0b"` が `/tmp/a` になる)。Linux / Android で先頭が NUL のパスは abstract namespace のアドレスなので、長さによらず標準にそのまま渡す。

### ディレクトリは呼ぶ少し前に引かれる

D を通す経路・D 無しの形・symlink-dir は R や symlink を作る時にソケットのディレクトリを実パスにし、connect だけの経路の proc-direct・relative・at-direct はディレクトリを先に引いて (開く、または実パスにする) から、ファイル名で bind / connect する。その間に途中のディレクトリが rename されたり symlink が張り替えられたりすると、引いた時点のディレクトリにソケットができる。widened・symlink-file・proc-held は標準と同じに振る舞う。

### relative は他のスレッドの chdir に弱い

sockat 自身は cwd を変えない。connect の後に cwd が変わっていたら、結果を捨てて errno の無い `OSError` を返す。変えて戻した場合は見えない。

### 他人も書けるソケットのディレクトリでの D 無しの形

sticky でなく他人も書けるソケットのディレクトリでは、D 無しの形の R を作ってから bind するまでの間に R を差し替えられ、別のディレクトリに bind しうる (同じ名前が既にあれば `EADDRINUSE` で、上書きはしない。D の中の R は D が 0700 なので差し替えられない)。

### 残骸が残ることがある

作ってから消すまでの間にプロセスが死ぬと、次が残る。

| 場所 | 残るもの |
|---|---|
| 置き場 (`TMPDIR` か `/tmp`。symlink-dir は `XDG_RUNTIME_DIR` も) | `sockat.XXXXXXXXXX` (D か、symlink-file や symlink-dir の symlink) |
| ソケットのディレクトリ | `.sockat.XXXXXXXXXX` (D か、D 無しの形の R) |

## 使ってはいけない書き方

sockat を使う時に、無関係なファイルを消したり、ファイルを消し損ねたりする書き方 ([仕様の「使ってはいけない書き方」](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#使ってはいけない書き方))。代わりの書き方のコードは試験 (`tests/test_docs.py`) がこの文書から読み出して動かす。`path` はソケットのパス。

### カーネルが返す綴りで消す

- 書き方: `os.unlink(socket.socket.getsockname(listener))` のように、カーネルが返す綴りでソケットのファイルを unlink / stat する
- なぜいけないか: 長いパスでは元のパスでない ([respelled](#respelled))。cwd や共有の置き場しだいで、作り直された別のファイルに当たりうる ([cwd-relative](#cwd-relative)、[recreatable-spelling](#recreatable-spelling))
- 代わりに: 元のパスを持っておいて使う。`sockat.ex.bind` の `discard()`

```python
import os

import sockat

listener = sockat.bind(path)
...
listener.close()
os.unlink(path)
```

### 相対パスで bind し、cwd を変えた後にその相対パスで消す

- 書き方: `sockat.bind("server.sock")` の後に `os.chdir(...)` し、`os.unlink("server.sock")` で消す
- なぜいけないか: 別の場所のファイルに当たる ([cwd-relative](#cwd-relative))
- 代わりに: bind の前に絶対パスにする。`discard()`

```python
import os

import sockat

path = os.path.abspath(path)
listener = sockat.bind(path)
os.chdir("/")
...
listener.close()
os.unlink(path)
```

### listener を閉じればソケットのファイルが消えると当てにする

- 書き方: `listener.close()` だけで後始末を終える
- なぜいけないか: 長いパスでは、標準が消す言語でも消えない ([respelled](#respelled))。Python の標準は短いパスでも消さない
- 代わりに: `discard()` か、元のパスで消す

```python
import sockat.ex

listener, discard, losses, route, skipped = sockat.ex.bind(path)
...
listener.close()
discard()
```

### discard() で listener も閉じると当てにする

- 書き方: `discard()` だけを呼んで listener を閉じない
- なぜいけないか: `discard()` はファイルを消すだけ (ハブの [DR-0015](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0015-ex-with-last-resort-and-diagnostics.md))
- 代わりに: listener は自分で閉じる

```python
import sockat.ex

listener, discard, losses, route, skipped = sockat.ex.bind(path)
...
discard()
listener.close()
```

### route・skipped・reason の文字列で分岐する

- 書き方: `if route == "symlink-dir":` のように、`sockat.ex` の `route`・`skipped`・`reason` の文字列で分岐する
- なぜいけないか: 診断用で、中身を約束しない (経路は増えたり変わったりする)
- 代わりに: `losses` で分岐する

```python
import sockat.ex

listener, discard, losses, route, skipped = sockat.ex.bind(path)
if sockat.ex.Loss.RECREATABLE_SPELLING in losses:
    ...
listener.close()
discard()
```

### fork を通るプログラムで、他のスレッドが waitpid(-1) で子を刈る

sockat-py には fork の経路が無いので起きない ([forked](#forked))。

## エラー

| 状況 | エラー |
|---|---|
| ディレクトリが無い / connect 先が無い | `FileNotFoundError` (`ENOENT`) |
| 長いパスのディレクトリ部分がディレクトリでない | `NotADirectoryError` (`ENOTDIR`) |
| bind 先に既にファイルがある | `OSError` (`EADDRINUSE`) |
| 誰も listen していないソケットファイル | `ConnectionRefusedError` (`ECONNREFUSED`) |
| backlog が満杯 | Linux はブロックしない connect で `BlockingIOError` (`EAGAIN`。ブロックする connect は待つ)、macOS は `ConnectionRefusedError` |
| どの経路にも収まらない ([どこから届かなくなるか](#どこから届かなくなるか)) | `OSError` (`ENAMETOOLONG`) |
| 標準にそのまま渡せない長さのパスで、名前で終わらない | `ValueError` |
| 先頭以外に NUL を含むパス | `ValueError` |
| relative の経路の途中、または相対パスの `sockat.ex.bind` の途中で cwd が変わった | errno の無い `OSError` |

macOS では、相手の backlog が満杯の時も connect は `ECONNREFUSED` を返す。`ECONNREFUSED` を見てソケットファイルを消すような処理は、macOS では生きている listener のソケットファイルを消しうる (カーネルの振る舞いで、標準でも同じ)。

## asyncio

戻り値は標準の `socket.socket` なので、`sock=` を受け取る API にそのまま渡せる。

```python
server = await asyncio.start_unix_server(handle, sock=sockat.bind(path))

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- 引数を足さずに、短いパスと同じに動く。Python 3.13 以降の asyncio は、server を閉じる時に元のパスのソケットファイルを消す (`cleanup_socket`。listener の `getsockname()` が元のパスを返すので、それで消す)。3.11 / 3.12 の asyncio は消さない (標準と同じ)
- transport の `get_extra_info('sockname')` (server 側) / `get_extra_info('peername')` (client 側) も元のパス
- `sockat.ex` の `listener` / `sock` も同じに渡せる。`discard()` は server の動作中に呼んでも、閉じた後に呼んでもよい

`connect_nonblocking` は相手を待たないので、イベントループから直接呼べる。`bind` も相手を待たない (thread-cwd ではスレッドの終わりを待つが、bind してすぐ終わる)。

## 確かめていないこと

- Android と iOS は動かしていない。Linux / macOS と同じ経路を使う
- FreeBSD・NetBSD・OpenBSD・illumos は CI の VM で試験を回している
- ブロックしない connect が backlog の満杯で返すエラーは、Linux と macOS でだけ確かめた
- 最終手段の symlink-dir を FreeBSD で通す試験は無い (at を使えなくできないので、symlink-dir に進む所を作れない)
- FreeBSD・NetBSD・OpenBSD・illumos の bind が symlink-file を辿るかは確かめていない (CI の VM で試験が見分ける)
