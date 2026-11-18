# sockat マニュアル

> [English](./MANUAL.md) | 日本語

sockat-py の使い方と、長いパスにどう届くか、長いパスで標準と違う点のリファレンス。言語によらない振る舞いの正本は [sockat の仕様](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)、Python 版の設計の判断は [DR-0001](./decisions/DR-0001-python-port-design.md) と [DR-0004](./decisions/DR-0004-own-directory-route.md) にある。

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
| `sockat.bind_ex(path)` | 同上 + ソケットファイルの後始末 | `(listener, discard)` ([後述](#bind_ex)) |
| `sockat.connect(path)` | `socket()` → `connect(path)` | `socket.socket` (ブロッキング) |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | 非ブロッキングの `socket.socket` |

- 標準が受け付ける長さのパスでは、戻り値は素の `socket.socket`。長いパスでは、アドレスを元のパスで返す `socket.socket` の subclass ([後述](#アドレスは元のパスを返す))
- `path` は `str` / `bytes` / `os.PathLike` (標準の `bind` / `connect` は `os.PathLike` を受けないが、sockat は受ける)
- 標準が受け付ける長さ ([OS ごとの値](#長いパスへの届き方)) のパスは標準をそのまま呼ぶ
- どの経路のソケットも `socket.socket(AF_UNIX, SOCK_STREAM)` で作るので、標準と同じ設定になる (close-on-exec、`socket.setdefaulttimeout` の値、listen の既定の backlog)
- ソケットファイルは渡したパスにできる。ソケットを閉じても消えない (標準と同じ)。消す時は `bind_ex` の `discard()` か、元のパスで `os.unlink(path)` (unlink には `sun_path` の上限が無い)
- backlog を変えたい時は、返った listener で `listen(n)` し直す

## connect と connect_nonblocking

- **`connect`** はブロックする。標準の `connect` と同じく `settimeout` の値に従う
- **`connect_nonblocking`** はイベントループで使う。ソケットを connect の前に非ブロッキングにし、そのまま返す。相手の backlog が満杯なら待たずに失敗する (Linux は `BlockingIOError`、macOS は `ConnectionRefusedError`)。Unix domain の connect は呼び出しの中で終わるか失敗するので、後で待つものは無い

## bind_ex

```python
listener, discard = sockat.bind_ex(path)
try:
    ...
finally:
    listener.close()
    discard()
```

- `listener` は `sockat.bind(path)` と同じ
- bind が通った直後 (listen の前) に、ソケットファイルの realpath と `(st_dev, st_ino)` を覚える。覚えられなければ listener を閉じて例外を返す
- `discard()` は、その realpath のファイルがまだ同じファイルの時だけ消す。無い時、別のファイルに置き換わっている時は何もしない
- 確かめてから消すまでの間の置き換えは防げない (POSIX に inode を指定して消す手段が無い)
- `discard()` は何度呼んでもよい。listener は閉じない (閉じるのは利用者。asyncio に渡したら asyncio)
- ファイルが無い以外のエラー (ディレクトリの `EACCES` 等) は `OSError`
- 短いパスでも同じに動く。Linux の abstract のアドレスでは何もしない
- 名前は仮 ([DR-0004](./decisions/DR-0004-own-directory-route.md))

## 長いパスへの届き方

標準が受け付ける長さ (`sun_path` − 1) のパスは標準にそのまま渡す。

| OS | 標準にそのまま渡す長さ |
|---|---|
| Linux / Android / illumos | 107 バイトまで |
| macOS / iOS / FreeBSD / NetBSD / OpenBSD | 103 バイトまで |

それを超えるパスは、まずソケットのディレクトリを引き (無ければ `ENOENT`)、OS ごとに次の順に試し、最初に使えた経路で bind / connect する。どの経路も呼び出し元のプロセスの cwd も呼び出し元のスレッドの cwd も変えない。

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` を使える) | [スレッド cwd](#手段) → [/proc 版](#手段) | [スレッド cwd](#手段) → [/proc 版](#手段) → [/proc](#予備の経路) → [fd で掴む形](#予備の経路) → [symlink](#予備の経路) |
| Linux / Android (`/proc` を使えない) | [スレッド cwd](#手段) | [スレッド cwd](#手段) → [相対パス](#予備の経路) → [symlink](#予備の経路) |
| FreeBSD | [bindat](#手段) | [connectat](#手段) → [connectat (ソケットのディレクトリ)](#予備の経路) |
| macOS / iOS | [スレッド cwd](#手段) | [スレッド cwd](#手段) → [相対パス](#予備の経路) → [広げた sockaddr](#予備の経路) → [symlink](#予備の経路) |
| NetBSD / OpenBSD / illumos / その他の Unix | なし (`ENAMETOOLONG`) | [相対パス](#予備の経路) → [symlink](#予備の経路) |

- スレッド cwd・/proc 版・bindat / connectat は[中心の経路](#中心の経路)の手段
- 仕様の fork の経路は作っていない。Python から fork した子は Python を実行するので、仕様が求める「子の区間は async-signal-safe」を満たせないため ([DR-0001](./decisions/DR-0001-python-port-design.md))。仕様で bind が fork に進む所は `ENAMETOOLONG` になる (ハブの D12-Q3 で裁定待ち)
- 順番の最後まで届かなかったパスは `ENAMETOOLONG` になる

### 中心の経路

1. 置き場に自分専用のディレクトリ D (mode 0700) を作り、fd で開いて掴む
2. D が自分の物 (ディレクトリ、持ち主が自分、group と other に権限が無い) か `fstat` で確かめ、0700 にそろえる
3. D の中に、ソケットのディレクトリの realpath を指す乱数 10 文字の symlink R を作る
4. D の fd を基準に `R/<ファイル名>` で bind / connect する
5. 成功でも失敗でも R と D を消す

- ディレクトリの深さによらず届く。綴り `R/<ファイル名>` に収まるファイル名は Linux 96、Apple の OS / FreeBSD 92 バイトまで
- 確かめに落ちた D は他人の物とみなし、中身も含めて触らない
- 長さと手段を使えるかは D を作る前に確かめる。使えなければ何も作らずに次へ進む

#### D の置き場

| 順 | 置き場 | D の名前 |
|---|---|---|
| 1 | `${TMPDIR:-/tmp}` (相対パスも可) | `sockat.XXXXXXXXXX` |
| 2 | `/tmp` (1 で作れない時だけ。1 と同じなら飛ばす) | `sockat.XXXXXXXXXX` |
| 3 | ソケットのディレクトリ (2 でも作れない時だけ) | `.sockat.XXXXXXXXXX` |

- 置き場の安全 (sticky 等) と長さは問わない。D を fd で掴み、綴りに置き場が出ないため
- D の開き方は、Linux が `O_PATH`、macOS が `O_SEARCH` (umask が持ち主の読み取りを削っても開ける)、他は `O_RDONLY`

#### 手段

| 手段 | OS | やり方 | 使えない時 |
|---|---|---|---|
| スレッド cwd | Linux / Android | 使い捨ての `threading.Thread` で ctypes の `unshare(CLONE_FS)` → `os.fchdir(D)` → `R/<ファイル名>` | seccomp の filter が掛かっている (docker の既定、systemd の `SystemCallFilter=` 等。filter が `unshare` でプロセスを kill しうるので呼ばない)、`unshare` を断られた、スレッドを作れない |
| スレッド cwd | macOS / iOS | 使い捨ての `threading.Thread` で ctypes の `pthread_fchdir_np(D)` (libSystem の非公開 API) → `R/<ファイル名>` | 関数が無い・失敗した、スレッドを作れない |
| bindat / connectat | FreeBSD | ctypes の `bindat(2)` / `connectat(2)` に D の fd と `R/<ファイル名>` | ファイル名 > 92 |
| /proc 版 | Linux / Android | `/proc/self/fd/<D の fd>/R/<ファイル名>` を標準に渡す | `/proc/self/fd/<fd>` を引けない (`/proc` の無い chroot や最小のコンテナ)、綴りが 107 バイトを超える (ファイル名およそ 79 まで) |

- スレッド cwd では、プロセスの cwd も他のスレッドの cwd も変わらない。connect した相手から見える peer の pid も呼び出し元のまま
- Linux でスレッド cwd が使えない時は、同じ D で /proc 版を試す

### 予備の経路

connect 系だけが、中心の経路を使えない時 (手段が無い、D を作れない、ファイル名が収まらない) に OS ごとの順で試す。

| 経路 | やり方 | 使える条件 |
|---|---|---|
| /proc | ソケットのディレクトリの fd で `/proc/self/fd/<fd>/<ファイル名>` を標準に渡す | Linux / Android。ソケットのディレクトリを開け、綴りが 107 バイト以内 (ファイル名およそ 90 まで) |
| fd で掴む形 | 中心の経路と同じ D を作り、その中にソケットのファイルを指す symlink `s` を作って `/proc/self/fd/<D の fd>/s` で connect し、消す | Linux / Android。D をどこかに作れる ([DR-0002](./decisions/DR-0002-unsafe-base-held-by-fd.md)) |
| 相対パス | cwd からの相対パスを標準に渡す。文字列を書き直すだけで `chdir` しない | 元のパスが絶対パスで、相対パスが標準にそのまま渡せる長さ |
| 広げた sockaddr | ctypes で libc の `connect(2)` に、`sun_path` を超えて 253 バイトまで入る sockaddr で元のパス (無理なら相対パス) を渡す | macOS / iOS。パスが 253 バイト以内で、カーネルが受け付ける |
| symlink | ソケットのファイルを指す `sockat.XXXXXXXXXX` を置き場に作り、それを渡して、すぐ消す | 安全な置き場があり、綴りが収まる (macOS は広げた sockaddr で 253 バイトまで) |
| connectat (ソケットのディレクトリ) | ctypes の `connectat(2)` にソケットのディレクトリの fd とファイル名 | FreeBSD。ソケットのディレクトリを開け、ファイル名が 103 バイト以内 |

- 相対パス: パスが cwd (`getcwd`、または cwd と同じディレクトリを指す `$PWD`) の下にあれば前を削る。そうでなければ両方を実パスに揃えて `..` で上る綴りを作る。短い方を使う
- 相対パス: 元と同じディレクトリを指すことを確かめてから使い、connect の後に cwd が変わっていないことも確かめる ([後述](#相対パスの経路は他のスレッドの-chdir-に弱い))
- symlink の置き場の候補は `TMPDIR` (絶対パスで `/tmp` と違う時)、`/tmp` の順
- symlink の**安全な置き場**はディレクトリで、次のどちらか。満たさない候補には何も作らず次へ進む
  - sticky bit が立っていて、持ち主が root か自分 (`/tmp` がこれ)
  - 持ち主が自分で、group と other に書き込み権限が無い (macOS の `$TMPDIR` がこれ)
- bind は /proc (D を通さない形)・相対パス・広げた sockaddr・置き場の symlink を使わない。綴りが後で無関係なファイルに当たるか (/proc は閉じた fd の番号の使い回し、相対パスは cwd の変化)、listener のアドレスが途中で切れて標準の `getsockname` が読み過ぎるか (広げた sockaddr)、中心の経路で足りる (symlink) ため ([DR-0004](./decisions/DR-0004-own-directory-route.md))

### 経路の失敗の扱い

経路が実際に bind / connect して失敗した時 (`ENOENT`、`EADDRINUSE`、`ECONNREFUSED` 等) は、そのエラーを返して次の経路も次の置き場も試さない。次へ進むのは、その経路自体が使えない時 (綴りが収まらない、D や R を作れない、手段が使えない、`/proc` を使えない、カーネルが広げた sockaddr を受け付けない、symlink の置き場が安全でない・symlink を作れない) だけ。

### どこから届かなくなるか

標準にそのまま渡せない長さのパスが `ENAMETOOLONG` になる時 (ディレクトリがあり、D を作れる前提。数字はバイト)。

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` を使える) | ファイル名 > 96 (スレッド cwd を使えなければおよそ 79) | 届く (fd で掴む形) |
| Linux / Android (`/proc` を使えない) | ファイル名 > 96、またはスレッド cwd を使えない | 中心の経路が使えず (ファイル名 > 96 かスレッド cwd を使えない)、相対パス > 107、symlink も使えない |
| FreeBSD | ファイル名 > 92 | ファイル名 > 103 |
| macOS / iOS | ファイル名 > 92、またはスレッド cwd を使えない | 中心の経路が使えず (ファイル名 > 92 かスレッド cwd を使えない)、元のパスも相対パスも > 253、symlink も使えない |
| NetBSD / OpenBSD | 長いパス全部 | 相対パス > 103、symlink も使えない |
| illumos | 長いパス全部 | 相対パス > 107、symlink も使えない |

- D をどこにも作れない時、bind は長いパス全部が `ENAMETOOLONG`。connect は予備の経路の分だけ届く (fd で掴む形は使えない)
- symlink を使えないのは、symlink を作れる安全な置き場が無い時 (`TMPDIR` も `/tmp` も使えないサンドボックス等)
- パス全体が `PATH_MAX` (macOS 1024、Linux 4096 バイト) を超えるパスは、どの経路でも `ENAMETOOLONG` になる (他のシステムコールでも開けないパスで、sockat の対象外)

## 長いパスで標準と違う点

### アドレスは元のパスを返す

長いパスでは、カーネルが返すアドレス (`getsockname(2)` / `getpeername(2)`) は元のパスでなく、bind の時にカーネルに渡した綴りになる。sockat が返すソケットは、それを元のパスに差し替える。

| ソケット | 元のパスを返すもの | それ以外 |
|---|---|---|
| `bind` / `bind_ex` の listener | `getsockname()` | 標準のまま |
| listener の `accept()` が返すソケット | `getsockname()` | `getpeername()` は標準のまま (相手の綴り。たいてい空) |
| `connect` / `connect_nonblocking` のソケット | `getpeername()` | `getsockname()` は標準のまま (たいてい空) |

- 元のパスは `os.fsdecode` した `str` (標準がパスのアドレスを返すのと同じ型)。相対パスで渡したら相対パスのまま
- `dup()` したソケットも同じに返す。`socket.fromfd` 等で fd から作り直したソケットは標準の型なので、カーネルの綴りを返す
- これで、アドレスを読み返す上位 (asyncio の後始末、`repr`、ログ) が元のパスで動く ([DR-0003](./decisions/DR-0003-report-the-given-path.md))

カーネルが返す綴りは次の通り (`socket.socket.getsockname(s)` のように標準の実装を呼ぶと見える)。R は bind の後に消えているので、この綴りを後でどこで解決しても何にも当たらない。

| bind の手段 | 返るもの |
|---|---|
| スレッド cwd、bindat | `R/<ファイル名>` |
| /proc 版 | `/proc/self/fd/<fd>/R/<ファイル名>` |

他のプログラムが広げた sockaddr で bind した listener (macOS の Node.js 等) に connect した時の `getpeername()` は途中で切れたパスで、macOS の CPython では切れた後ろに無関係なバイトが付くことがある (標準の `getpeername` の振る舞い。取得で例外にはならない。[原因](./findings/2026-11-18-cpython-getsockname-widened-sockaddr.md))。

### 名前で終わらないパスは ValueError

標準にそのまま渡せない長さのパスで、最後がファイル名でないもの (`dir/`、`dir/.`、`dir/..`、`..`) は、何も作らずに `ValueError` を返す。それより短いパスは標準にそのまま渡すので、カーネルのエラーになる。

### NUL を含むパスは ValueError

先頭以外に NUL を含むパスは、長さによらず `ValueError` を返す。標準は NUL で黙って切り詰めて別のパスに bind する (`b"/tmp/a\0b"` が `/tmp/a` になる)。Linux / Android で先頭が NUL のパスは abstract namespace のアドレスなので、長さによらず標準にそのまま渡す。

### ディレクトリは呼ぶ少し前に引かれる

中心の経路は R を作る時にソケットのディレクトリを実パスにし、予備の経路の /proc・相対パス・connectat はディレクトリを先に引いて (開く、または実パスにする) から、ファイル名で bind / connect する。その間に途中のディレクトリが rename されたり symlink が張り替えられたりすると、引いた時点のディレクトリにソケットができる。広げた sockaddr・symlink・fd で掴む形は標準と同じに振る舞う。

### 相対パスの経路は、他のスレッドの chdir に弱い

sockat 自身は cwd を変えない。connect の後に cwd が変わっていたら、結果を捨てて errno の無い `OSError` を返す。変えて戻した場合は見えない。

### 残骸が残ることがある

作ってから消すまでの間にプロセスが死ぬと、次が残る。

| 場所 | 残るもの |
|---|---|
| 置き場 (`TMPDIR` か `/tmp`) | `sockat.XXXXXXXXXX` (D か、connect の symlink) |
| ソケットのディレクトリ | `.sockat.XXXXXXXXXX` (D) |

- macOS で umask が持ち主の検索権限を削っている (root 以外で 0777 / 0177 等) と、作った D を開けず、自分の物か確かめられないので消さずに残す。`O_RDONLY` で開く OS では読み取り権限で同じ

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
| 相対パスの経路の途中で cwd が変わった | errno の無い `OSError` |

macOS では、相手の backlog が満杯の時も connect は `ECONNREFUSED` を返す。`ECONNREFUSED` を見てソケットファイルを消すような処理は、macOS では生きている listener のソケットファイルを消しうる (カーネルの振る舞いで、標準でも同じ)。

## asyncio

戻り値は標準の `socket.socket` なので、`sock=` を受け取る API にそのまま渡せる。

```python
server = await asyncio.start_unix_server(handle, sock=sockat.bind(path))

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

- 引数を足さずに、短いパスと同じに動く。Python 3.13 以降の asyncio は、server を閉じる時に元のパスのソケットファイルを消す (`cleanup_socket`。listener の `getsockname()` が元のパスを返すので、それで消す)。3.11 / 3.12 の asyncio は消さない (標準と同じ)
- transport の `get_extra_info('sockname')` (server 側) / `get_extra_info('peername')` (client 側) も元のパス
- `bind_ex` の listener も同じに渡せる。`discard()` は server の動作中に呼んでも、閉じた後に呼んでもよい

`connect_nonblocking` は相手を待たないので、イベントループから直接呼べる。`bind` も相手を待たない (スレッド cwd ではスレッドの終わりを待つが、bind してすぐ終わる)。

## 確かめていないこと

- Android と iOS は動かしていない。Linux / macOS と同じ経路を使う
- FreeBSD・NetBSD・OpenBSD・illumos は CI の VM で試験を回している
- ブロックしない connect が backlog の満杯で返すエラーは、Linux と macOS でだけ確かめた
