# sockat マニュアル

> [English](./MANUAL.md) | 日本語

sockat-py の使い方と、長いパスにどう届くか、長いパスで標準と違う点のリファレンス。言語によらない振る舞いの正本は [sockat の仕様](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md) (経路の作り方は [SPEC-routes](https://github.com/kawaz/sockat/blob/main/docs/SPEC-routes-ja.md)、どこまで届くかは[届く範囲の表](https://github.com/kawaz/sockat/blob/main/pages/reach/README-ja.md))、Python 版の設計の判断は [DR-0001](./decisions/DR-0001-python-port-design.md) にある。

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
| `sockat.ex.bind(path, options)` など | 同じ + 綴りの性質・片付け・診断 | `Bound` / `Connected` / `Report` ([sockat.ex](#sockatex)) |
| `sockat.ex.resolve_for_bind(path, options)` / `resolve_for_connect` | 標準に自分で渡す短い綴り | `ForBind` / `ForConnect` ([resolve](#resolve)) |
| `sockat.ex.sweep()` | — | `None` ([sweep](#sweep)) |

- 標準が受け付ける長さのパスでは、戻り値は素の `socket.socket`。長いパスでは、アドレスを元のパスで返す `socket.socket` の subclass ([アドレスは元のパスを返す](#アドレスは元のパスを返す))
- `path` は `str` / `bytes` / `os.PathLike`
- 標準が受け付ける長さ ([OS ごとの値](#長いパスへの届き方)) のパスは標準をそのまま呼び、何も足さない (環境変数も読まない)
- どの経路のソケットも `socket.socket(AF_UNIX, SOCK_STREAM)` (DGRAM は `SOCK_DGRAM`) で作るので、標準と同じ設定になる (close-on-exec、`socket.setdefaulttimeout` の値、listen の既定の backlog)
- ソケットファイルは渡したパスにできる。ソケットを閉じても消えない (標準と同じ)。消す時は `sockat.ex.bind` の `file.remove()` か、元のパスで `os.unlink(path)` (unlink には `sun_path` の上限が無い)
- backlog を変えたい時は、返った listener で `listen(n)` し直す

## connect と connect_nonblocking

- **`connect`** はブロックする。標準の `connect` と同じく `settimeout` の値に従う
- **`connect_nonblocking`** はイベントループで使う。ソケットを connect の前に非ブロッキングにし、そのまま返す。相手の backlog が満杯なら待たずに失敗する (Linux は `BlockingIOError`、macOS は `ConnectionRefusedError`)。Unix domain の connect は呼び出しの中で終わるか失敗するので、後で待つものは無い
- どちらも何も残さない (link-file まで進んでも、作った E は connect の後すぐ消す)

## sockat.datagram

標準の DGRAM のうち、パスを取る関数と、それと組で使う `unbound` だけを写す ([仕様の「関数」](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#関数))。

```python
import sockat

server = sockat.datagram.bind(path)
client = sockat.datagram.unbound()
sockat.datagram.connect(client, path)
client.send(b"hello")
```

- `bind` は stream の bind と同じ経路を試す (listen はしない)。綴りが生きている間は、長いパスに bind した送り手への返信 (`recvfrom` で得たアドレスへの `sendto`) が届く
- `connect` は stream の connect と同じ経路を、渡したソケットで試す。失敗してもソケットは閉じない (閉じるのは利用者)
- `send_to` (パスへの送信) と SEQPACKET はまだ無い (仕様で未確定)

## sockat.ex

`import sockat` では読み込まれない。使う人が `import sockat.ex` で選ぶ。`sockat.ex.connect(...)` のようにモジュールを通して呼ぶ (`from sockat.ex import connect` と取り込むと既定の `sockat.connect` や標準の `socket.connect_ex` と紛れる)。

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
| `sockat.ex.bind(path, options=None)` | `Bound(socket, file, spelling, route, base, skipped)` |
| `sockat.ex.connect(path, options=None)` / `connect_nonblocking` | `Connected(socket, route, skipped)` |
| `sockat.ex.datagram.bind(path, options=None)` | `Bound` (socket は DGRAM) |
| `sockat.ex.datagram.connect(sock, path, options=None)` | `Report(route, skipped)` |
| `sockat.ex.resolve_for_bind(path, options=None)` | `ForBind(path, spelling, route, base, skipped)` と `cleanup()` |
| `sockat.ex.resolve_for_connect(path, options=None)` | `ForConnect(path, route, base, skipped)` と `cleanup()` |
| `sockat.ex.sweep()` | `None` |

- 既定の呼び出しと同じ経路を同じ順に試す
- `Bound` / `Connected` / `Report` は名前付きタプルで、上の順に分解できる

### Options

| 項目 | 中身 |
|---|---|
| `usable_cross_uid: bool = False` | 別の uid の相手も辿れる綴りを求める。B を、途中が全部 other に x を持つ置き場 (`/tmp`) から取り、procB (`/proc/<pid>/fd/...`) を使わない。満たせなければ満たさない綴りに落とさず thread-cwd / at (`Dangling`) に進む。満たせたかは結果の `spelling` で分かる |
| `base: str \| bytes \| os.PathLike \| None = None` | B そのもののパスを指定する (アプリが自分で管理する場所。例 `/tmp/app-<uid>/sockat`)。無ければ作り (親は要る)、あれば確かめる。外れたらエラーで、自動の置き場に落とさない ([エラー](#エラー)) |

### 綴りの性質 (Spelling)

bind の綴り (カーネルが覚え、`getsockname` で返す物) を後で使えるかを、`Bound.spelling` / `ForBind.spelling` で返す。分岐はこれで行う。

| 値 | 綴り | 経路 |
|---|---|---|
| `Original()` | 元の絶対パス | standard |
| `CwdRelative()` | 元の相対パス。cwd を変えると別の所を指す (標準が元から持つ危険だが、知らせる) | standard |
| `Linked(removable, until, usable_cross_uid, usable_with_private_tmp)` | B・D・E を通り、元のパスと同じ所を指す | link-dir、link-file |
| `Dangling()` | 片付け済みの E を含む。どこにも届かない | thread-cwd、at |

| `Linked` の属性 | 中身 |
|---|---|
| `removable` | 綴りで unlink すると実体が消えるか。link-dir は `True`、link-file は `False` (最後の symlink は辿られず E が消える) |
| `until` | `Until.PROCESS` (procB: プロセスが終われば辿れない) か `Until.CLEANUP` (B の実パス: E は `cleanup()` まで) |
| `usable_cross_uid` | 別の uid の相手が辿れるか |
| `usable_with_private_tmp` | 自分か相手が `/tmp` を隔離していても (systemd の `PrivateTmp` 等) 辿れるか。綴りが `/tmp`・`/var/tmp` を通らなければ `True` |

```python
match bound.spelling:
    case sockat.ex.Linked(removable=True):
        ...  # unlinking by the spelling removes the socket file
    case sockat.ex.Original() | sockat.ex.CwdRelative():
        ...  # the path itself
    case _:
        ...  # it does not: remove by the path or file.remove()
```

- 値は増えうる。知らない値は「綴りを使えない」と読む

### remove と cleanup

`Bound.file` (`SocketFile`) の 2 つの口。どちらも呼んだ時だけ働き (listener を閉じても、ガベージコレクションでも消さない)、1 回だけ働く。

| 口 | すること |
|---|---|
| `file.remove()` | bind したソケットのファイルを消し、`cleanup()` もする |
| `file.cleanup()` | 綴りが通る E (link-dir・link-file) を消す。ソケットのファイルは残る。以後その綴りは使わない |

- `remove()` はファイルを消すだけで、listener を閉じない。閉じるのは利用者
- bind が通った直後 (listener を返す前) に、ソケットのファイルの realpath と `(st_dev, st_ino)` を覚え、そのパスの `(st_dev, st_ino)` が同じ時だけ消す。覚えられなければ listener を閉じ、ファイルは消さずにエラーを返す
- 相対パスの時は、呼び出しの初めと覚えた後で cwd の `(st_dev, st_ino)` を比べ、違えば errno の無い `OSError` で listener を閉じる (間の cwd の変更で、元の cwd の無関係なファイルを覚えないため。変えて戻した場合は見えない)
- ファイルが既に無い時と、別のファイルに置き換わっていて消さない時も成功する。失敗するのは、確かめられない時 (`lstat` が `ENOENT` / `ENOTDIR` 以外で失敗) と、消そうとして失敗した時
- 置き換えを見分けるのは `(st_dev, st_ino)` だけで、約束するのは listener を開いている間の置き換えまで。閉じた後に消して作り直された物は、番号をすぐ使い回すファイルシステム (ext4、BSD の UFS / FFS) で見分けられない。**`remove()` は listener を閉じる前に呼ぶ**
- 確かめてから消すまでの間に、ソケットのディレクトリに書ける者がファイルをすり替えるのは防げない (inode を指定して消す手段が POSIX に無い)
- 既定の `sockat.bind` は `file` を返さないので、その E はプロセスが終わるまで残り、D と一緒に OS の一時ディレクトリの掃除で消える

### 診断とエラー

| 項目 | 中身 |
|---|---|
| `route` | 通った経路の名前 ([経路の名前](#経路の名前)) |
| `base` | bind の綴りが通る B (`"proc"`・`"sockdir"`・`"tmp"`・`"runtime"`・`"tmpdir"`・`"base"`)。B を通らない綴り (standard・thread-cwd・at) は `None`。`Connected` と `Report` には無い |
| `skipped` | 試して使えなかった経路の並び (試した順)。各要素は `Skipped(name, reason)`。経路の名前ごとに 1 件 |

- `route` より前に試した経路はすべて使えなかったもの (実際に bind / connect して失敗したら、そこでエラーを返して次を試さない)
- `route`・`base`・`skipped`・`reason` は診断用で、中身を約束しない (経路は増えたり変わったりする)。分岐には `spelling` を使う
- 失敗した時は、例外 (型と `errno` は既定の呼び出しと同じ) に `skipped` と `route` の属性を付ける。`route` は実際に bind / connect して失敗した経路の名前で、どの経路もそこまで行かなかった時 (全部が使えなかった `ENAMETOOLONG` 等) は `None`

### resolve

長いパスを、元のパスと同じ所を指し続ける短い綴りに言い換えて返す。綴りはただのパスなので、ソケットの型と操作を問わず、標準に自分で渡して使える。

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

| | `resolve_for_bind` | `resolve_for_connect` |
|---|---|---|
| 綴り | link-dir (`B/D/E/<ファイル名>`)、bind が symlink を辿ると実測した OS (macOS・NetBSD) では次に link-file (`B/D/E`) | link-file (`B/D/E`)。ファイル名の長さによらない |
| 返す物 | `ForBind` (`path`・`spelling`・`cleanup()`) | `ForConnect` (`path`・`cleanup()`。`remove()` は持たない) |
| 使えない時 | `ENAMETOOLONG` (thread-cwd・at でしか届かない長さ) | `ENAMETOOLONG` (B を作れない、綴りが収まらない) |

- 標準に渡す長さ以下のパスはそのまま返す (`str` / `bytes` は渡した型のまま)
- `ENAMETOOLONG` の所も届かせたい時は `bind` / `connect` を使う
- 同じパスへの connect を繰り返す時は、`resolve_for_connect` で 1 度作った綴りを何度も使い、最後に `cleanup()` する
- `ForBind` の `remove()` はまだ無い (仕様で未確定)。bind したファイルは元のパスで消す

### sweep

`sockat.ex.sweep()` は、自動の置き場の B の中から、持ち主のプロセスが終わった D (ロックの外れた D) を消す。生きている他のプロセスの D は消さない。呼んだ時だけ働く (sockat が自分で呼ぶことは無い)。B は作らず、B の中の D 以外には触らない。

## 長いパスへの届き方

標準が受け付ける長さ (`sun_path` − 1) のパスは標準にそのまま渡す (経路 standard)。

| OS | 標準にそのまま渡す長さ |
|---|---|
| Linux / Android / illumos | 107 バイトまで |
| macOS / iOS / FreeBSD / NetBSD / OpenBSD | 103 バイトまで |

それを超えるパスは、次の順に経路を試し、最初に使えた経路で bind / connect する。

| OS | bind | connect / connect_nonblocking |
|---|---|---|
| Linux / Android (`/proc` を使える) | link-dir (procB → B の実パス) → thread-cwd | proc-direct → thread-cwd-direct → link-file |
| Linux / Android (`/proc` を使えない) | link-dir (B の実パス) → thread-cwd | relative → thread-cwd-direct → link-file |
| macOS / iOS | link-dir (B の実パス) → link-file → thread-cwd | relative → widened → thread-cwd-direct → link-file |
| FreeBSD | link-dir (B の実パス) → at | at-direct → link-file |
| NetBSD | link-dir (B の実パス) → link-file | relative → link-file |
| OpenBSD / illumos | link-dir (B の実パス) | relative → link-file |
| その他の Unix | link-dir (B の実パス) → link-file (辿るかを実行時に見分ける) | relative → link-file |
| Windows | 対象外 (import で `ImportError`) | 同左 |

- その OS に手段の無い経路 (NetBSD・OpenBSD・illumos の thread-cwd、FreeBSD 等の thread-cwd-direct) は `skipped` に理由付きで入る
- どの経路も呼び出し元のプロセスとスレッドの cwd を変えないので、マルチスレッドのプログラムから使える。fork はしない
- どの経路にも収まらないパスは、標準で長すぎるパスと同じく `ENAMETOOLONG` (errno 付きの `OSError`) になる

### 置き場 B・D・E

長いパスの bind の綴りは、消さずに置いておくディレクトリと symlink を通る。

| | B | D | E |
|---|---|---|---|
| 何か | 置き場の中の実在のディレクトリ (0711) | B の中のディレクトリ (0711、名前は乱数) | symlink |
| 作る単位 | 利用者ごと。プロセスをまたいで使い回す。1 つのプロセスが使う自動の置き場の B は 1 つ (`usable_cross_uid` の時だけもう 1 つ使いうる) | (プロセス, B) ごとに 1 つ。綴りを残す bind と `resolve_for_connect` だけが作る | resolve ごと。thread-cwd・at と既定の connect の link-file は呼び出しごと |
| 生きている間 | 消さない | プロセスが flock の共有ロックを掛けた fd を持ち続ける | resolve の E は `cleanup()` まで |
| 片付け | sockat はしない | sockat はしない。死んだプロセスの D は OS の一時ディレクトリの掃除で消える。`sweep()` を呼んだ時だけ、ロックの外れた D を消す | resolve の E は `cleanup()`。呼び出しごとの E は呼び出しの中で消す |

B の置き場は次の順で最初に作れた所 (`Options.base` を指定した時はそこだけ)。

| 順 | 置き場 | B の名前 | 診断の `base` (実パスで綴った時) |
|---|---|---|---|
| 1 | `/tmp` | `sockat-<uid>` | `tmp` |
| 2 | `$XDG_RUNTIME_DIR` (絶対パスの時) | `sockat` | `runtime` |
| 3 | `$TMPDIR` (絶対パスの時) | `sockat-<uid>` | `tmpdir` |
| 4 | ソケットのディレクトリ | `.sockat.XXXXXXXXXX` | `sockdir` (procB でだけ使える) |

- 置き場は realpath にしてから使う。ルートから置き場までのディレクトリがどれも、持ち主が root か自分で他人が書けない、か sticky でなければ使わない (OpenSSH の StrictModes と同じ考え)
- B は `mkdirat` で作り、`O_NOFOLLOW` で開いて実ディレクトリで持ち主が自分なことを確かめてから 0711 にする。既にあれば同じく確かめ (他人が書けないことも)、外れたら `sockat-<uid>.XXXXXXXXXX` のような乱数の名前の B を横に作る
- D は乱数の名前で作り、flock の共有ロックを掛けた fd をプロセスが持ち続ける。`sweep()` は排他ロックを取れた D だけを消す。fork した子は親の B と D を使わず、自分の D を作る
- B の綴りは procB (`/proc/<pid>/fd/<B の fd>`、`/proc` を使える Linux) を先に、無理なら B の実パス

### 経路の名前

名前は「手段 + 何についてか」。接尾辞 `-direct` は E を通さずファイル名で渡す形。

| 経路 | 操作 | やり方 |
|---|---|---|
| standard | bind、connect | 標準にそのまま渡す |
| link-dir | bind | D の中にソケットのディレクトリの realpath を指す E を作り、`B/D/E/<ファイル名>` で bind する。E は `cleanup()` まで残す |
| link-file | bind、connect | ソケットのファイルを指す E を作る。bind と resolve は D の中の E を `B/D/E` で、既定の connect は B の直下の乱数の E を B の実パスの `B/E` で connect してすぐ消す。綴りにファイル名が出ないので、ファイル名の長さによらない。bind は、最後の要素の symlink を辿る OS (macOS と NetBSD で確認) でだけ試し、辿らないと確かめた OS (Linux / Android、FreeBSD、OpenBSD、illumos) では試さない。他の OS では、bind が失敗して元のパスに何も無ければ辿らなかったとみなして次へ進む |
| thread-cwd | bind | D の中にソケットのディレクトリを指す乱数の E を作り、使い捨てのスレッドだけ cwd を D に移して `E/<ファイル名>` で bind し、E をすぐ消す (Linux / Android は `unshare(CLONE_FS)` → `fchdir`、macOS / iOS は `pthread_fchdir_np`) |
| at | bind | thread-cwd と同じ綴りを、`bindat(2)` に D の fd で渡す (FreeBSD) |
| proc-direct | connect | `/proc/self/fd/<ソケットのディレクトリの fd>/<ファイル名>` で渡す (Linux / Android) |
| relative | connect | cwd からの相対パスで渡す。文字列を書き直すだけで cwd は変えない |
| widened | connect | macOS のカーネルが受け付ける、253 バイトまで入る sockaddr で渡す |
| at-direct | connect | `connectat(2)` にソケットのディレクトリの fd とファイル名を渡す (FreeBSD) |
| thread-cwd-direct | connect | 使い捨てのスレッドだけ cwd をソケットのディレクトリに移し、ファイル名で渡す (Linux / Android、macOS / iOS) |

- bind は proc-direct・relative・widened・at-direct を使わない。綴りが後で無関係なファイルに当たる (proc-direct、relative、at-direct) か、アドレスが途中で切れて読む側を壊す (widened)
- スレッドの cwd は、Linux で seccomp の filter が掛かっている時 (docker の既定、systemd の `SystemCallFilter=` 等) は使わない (`unshare` を呼ぶとプロセスが殺される filter がある)
- 使い捨てのスレッドが終わるまで呼び出し元は待つ。待つのが割り込まれても (`KeyboardInterrupt`)、スレッドが終わってから片付け、割り込みを投げ直す

### 経路の失敗の扱い

経路が実際に bind / connect して失敗したら (`ENOENT`、`EADDRINUSE`、`ECONNREFUSED` 等)、そのエラーを返して次の経路を試さない。次へ進むのは、経路自体が使えない時 (綴りが収まらない、B・D・E を作れない、手段が無い、`/proc` を使えない、widened をカーネルが受け付けない、link-file の bind をカーネルが辿らなかった、relative の綴りのディレクトリが元と一致しない) だけ。

### どこまで届くか

綴りの長さで届くファイル名が決まる (数字はバイト、Linux の sockaddr は 107、他は 103)。

| 経路 | 届くファイル名 |
|---|---|
| link-dir (procB) | 107 − (`/proc/<pid>/fd/<fd>/` + D 10 + E + 区切り)。おおよそ 75 |
| link-dir (B の実パス) | sockaddr − (B の実パス + D 10 + E + 区切り)。`/tmp/sockat-1000` でおおよそ 77、macOS の `/private/tmp/sockat-501` でおおよそ 66 |
| link-file | ファイル名によらない (綴り `B/D/E` が収まれば) |
| thread-cwd・at | sockaddr − 11 (Linux 96、他 92) |
| proc-direct | 107 − `/proc/self/fd/<fd>/` (およそ 90) |
| relative | 相対パス全体が sockaddr に入れば |
| widened | パス (か相対パス) 全体が 253 まで |
| at-direct・thread-cwd-direct | sockaddr |

どの条件でどこまで届くかの全体は[届く範囲の表](https://github.com/kawaz/sockat/blob/main/pages/reach/README-ja.md)にある。

## 長いパスで標準と違う点

### アドレスは元のパスを返す

- listener (と accept したソケット) の `getsockname()` と、connect したソケットの `getpeername()` は元のパスを返す。カーネルが返すのは bind の綴り (`/proc/<pid>/fd/<fd>/D/E/<ファイル名>`、`<B>/D/E/<ファイル名>`、`<B>/D/E`、`E/<ファイル名>`) だが、sockat が返すソケットはそれを元のパスに差し替える (`socket.socket` の subclass)
- 標準のソケットが失敗する所 (閉じた後の `EBADF`、listener の `getpeername` の `ENOTCONN`) は同じく失敗する
- `ss -x`・`lsof`・`/proc/net/unix` 等の運用の道具に出るアドレスは bind の綴り。link-dir と thread-cwd・at はファイル名が綴りの最後に残る。link-file の綴りにはファイル名が出ない
- DGRAM で `recvfrom` が返す送り手のアドレスはカーネルの綴り。綴りが生きている間は、そこへ `sendto` すれば届く
- illumos では connect した側の `getpeername` (カーネルの値) が、bind した綴りでなく connect に渡した綴りを返す

### 状態がアプリの置き場の外に増える

- B・D・E が `/tmp`・`$XDG_RUNTIME_DIR`・`$TMPDIR` に残る。既定の `bind` の E と死んだプロセスの D は OS の掃除まで残る (Linux の `/tmp` は 10 日、macOS の `/tmp` は再起動まで、`$XDG_RUNTIME_DIR` はログアウトまで)
- 生きている D が消されうる環境がある: systemd v242 より古い Linux (RHEL 8 等) の `/tmp` と、B が macOS の `$TMPDIR` にある時。綴りが辿れなくなるだけで危険はない
- exec をまたぐと D の flock の fd が閉じ、引き継いだ listener が生きていても D・E は `sweep()` や OS の掃除で消されうる。procB の綴りは exec の時点で辿れなくなる
- 片付けた後の綴りは使わない。B が `/tmp` にある時は、B が消えた後に他人が同じ名前で作り直しうる

### PrivateTmp の相手

相手の `/tmp` が隔離されていると (systemd の `PrivateTmp` 等)、`/tmp` を通る綴りを相手は辿れない。Linux では procB を先に使うので、まず起きない。辿れるかは `Linked.usable_with_private_tmp` で分かる。

### listener を閉じた時

Python の標準は listener を閉じてもソケットのファイルを消さない。sockat も消さない (`sockat.ex.bind` の listener は短いパスでも、消すのは `file.remove()` だけ)。

### ディレクトリを先に引く

link-dir と thread-cwd・at の E はソケットのディレクトリの realpath を指し、proc-direct・at-direct・thread-cwd-direct はディレクトリを引いてからファイル名で渡す。その間の rename や symlink の張り替えは反映されない。

### relative は他のスレッドの chdir に弱い

relative の途中で cwd が変わったままなら、結果を捨てて errno の無い `OSError` を返す (別の所へ届くことは無い)。変えて戻した場合は見えない。

### 名前で終わらないパス、NUL

- 標準にそのまま渡せない長さのパスで、名前で終わらないもの (`dir/`、`dir/.`、`..` で終わるもの) は、何も作らずに `ValueError` になる
- 先頭以外に NUL を含むパスは、長さによらず `ValueError` になる (標準は NUL で黙って切り詰める)。Linux で先頭が NUL のパス (abstract namespace) は標準にそのまま渡す。abstract namespace の無い OS では先頭の NUL も `ValueError`

## 使ってはいけない書き方

sockat を使う時に、無関係なファイルを消したり、ファイルを消し損ねたりする書き方 ([仕様の「使ってはいけない書き方」](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#使ってはいけない書き方))。代わりの書き方のコードは試験 (`tests/test_docs.py`) がこの文書から読み出して動かす。`path` はソケットのパス。

### Spelling を見ずに、カーネルが返す綴りで消す

- 書き方: `os.unlink(socket.socket.getsockname(listener))` のように、カーネルが返す綴りでソケットのファイルを unlink / stat する
- なぜいけないか: link-file の綴りでは E が消えるだけでファイルは残る。`Dangling` の綴りはどこにも届かない
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
- なぜいけないか: 片付けた後の `/tmp` の B は、他人が同じ名前で作り直しうる。procB の綴りはプロセスが終われば辿れない
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
- なぜいけないか: 別の場所のファイルに当たる (`CwdRelative`)
- 代わりに: bind の前に絶対パスにする。`file.remove()`

```python
import os

import sockat.ex

path = os.path.abspath(path)
bound = sockat.ex.bind(path)
os.chdir("/")
...
bound.file.remove()
bound.socket.close()
```

### listener を閉じればソケットのファイルが消えると当てにする

- 書き方: `listener.close()` だけで後始末を終える
- なぜいけないか: Python の標準は閉じても消さない。`sockat.ex.bind` の listener も閉じても消さない
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
- なぜいけないか: `remove()` はファイルを消すだけ
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
- なぜいけないか: 閉じた後に消して作り直された別のファイルを見分けられないことがある ([remove と cleanup](#remove-と-cleanup))
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
- なぜいけないか: 辿れない (procB は同じ uid だけ、0700 の置き場の下の綴りは自分だけ)
- 代わりに: `Options(usable_cross_uid=True)` を指定し、結果の `spelling` を確かめる

```python
import sockat.ex

bound = sockat.ex.bind(path, sockat.ex.Options(usable_cross_uid=True))
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
- なぜいけないか: 診断用で、中身を約束しない (経路は増えたり変わったりする)
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

| 状況 | エラー |
|---|---|
| ディレクトリが無い / connect 先が無い | `FileNotFoundError` (`ENOENT`) |
| 長いパスのディレクトリ部分がディレクトリでない | `NotADirectoryError` (`ENOTDIR`) |
| bind 先に既にファイルがある | `OSError` (`EADDRINUSE`) |
| 誰も listen していないソケットファイル | `ConnectionRefusedError` (`ECONNREFUSED`) |
| backlog が満杯 | Linux はブロックしない connect で `BlockingIOError` (`EAGAIN`。ブロックする connect は待つ)、macOS は `ConnectionRefusedError` |
| どの経路も使えない (綴りが収まらない、手段が無い) | `OSError` (`ENAMETOOLONG`) |
| `resolve_for_bind` で thread-cwd・at でしか届かない長さ、`resolve_for_connect` で link-file を使えない | `OSError` (`ENAMETOOLONG`) |
| `Options.base` の親が無い | `FileNotFoundError` (`ENOENT`) |
| `Options.base` (かその親) がディレクトリでない | `NotADirectoryError` (`ENOTDIR`) |
| `Options.base` の持ち主・権限の確かめに外れた (`usable_cross_uid` と一緒の時の、途中が other に x を持つかを含む) | `PermissionError` (`EPERM`) |
| 標準にそのまま渡せない長さのパスで、名前で終わらない | `ValueError` |
| 先頭以外に NUL を含むパス | `ValueError` |
| relative の経路の途中、または相対パスの `sockat.ex.bind` の途中で cwd が変わった | errno の無い `OSError` |

- `Options.base` のエラーでは自動の置き場に落とさない
- macOS では、相手の backlog が満杯の時も connect は `ECONNREFUSED` を返す。`ECONNREFUSED` を見てソケットファイルを消すような処理は、macOS では生きている listener のソケットファイルを消しうる (カーネルの振る舞いで、標準でも同じ)

## asyncio

戻り値は標準の `socket.socket` なので、`sock=` を受け取る API にそのまま渡せる。

```python
server = await asyncio.start_unix_server(handle, sock=sockat.bind(path))

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- 引数を足さずに、短いパスと同じに動く。Python 3.13 以降の asyncio は、server を閉じる時に元のパスのソケットファイルを消す (`cleanup_socket`。listener の `getsockname()` が元のパスを返すので、それで消す)。3.11 / 3.12 の asyncio は消さない (標準と同じ)
- transport の `get_extra_info('sockname')` (server 側) / `get_extra_info('peername')` (client 側) も元のパス
- `sockat.ex` の `socket` も同じに渡せる。`file.remove()` は server を閉じる前 (動作中) に呼ぶ

`connect_nonblocking` は相手を待たないので、イベントループから直接呼べる。`bind` も相手を待たない (thread-cwd ではスレッドの終わりを待つが、bind してすぐ終わる)。

## 確かめていないこと

- Android と iOS は動かしていない。Linux / macOS と同じ経路を使う
- FreeBSD・NetBSD・OpenBSD・illumos は CI の VM で試験を回している
- ブロックしない connect が backlog の満杯で返すエラーは、Linux と macOS でだけ確かめた
- FreeBSD の connect で link-file を使えるか (仕様で未確認)
