# sockat マニュアル

> [English](./MANUAL.md) | 日本語

sockat-py の使い方と、長いパスにどう届くか、長いパスで標準と違う点のリファレンス。言語によらない振る舞いの正本は [sockat の仕様](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)、Python 版の設計の判断は [DR-0001](./decisions/DR-0001-python-port-design.md) にある。

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

- `path` は `str` / `bytes` / `os.PathLike` (標準の `bind` / `connect` は `os.PathLike` を受けないが、sockat は受ける)
- 標準が受け付ける長さ ([OS ごとの値](#長いパスへの届き方)) のパスは標準をそのまま呼ぶ
- どの経路のソケットも `socket.socket(AF_UNIX, SOCK_STREAM)` で作るので、標準と同じ設定になる (close-on-exec、`socket.setdefaulttimeout` の値、listen の既定の backlog)
- ソケットファイルは渡したパスにできる。ソケットを閉じても消えない (標準と同じ)。消す時は元のパスで `os.unlink(path)` する (unlink には `sun_path` の上限が無い)
- backlog を変えたい時は、返った listener で `listen(n)` し直す

## connect と connect_nonblocking

- **`connect`** はブロックする。標準の `connect` と同じく `settimeout` の値に従う
- **`connect_nonblocking`** はイベントループで使う。ソケットを connect の前に非ブロッキングにし、そのまま返す。相手の backlog が満杯なら待たずに失敗する (Linux は `BlockingIOError`、macOS は `ConnectionRefusedError`)。Unix domain の connect は呼び出しの中で終わるか失敗するので、後で待つものは無い

## 長いパスへの届き方

標準が受け付ける長さ (`sun_path` − 1) のパスは標準にそのまま渡す。

| OS | 標準にそのまま渡す長さ |
|---|---|
| Linux / Android / illumos | 107 バイトまで |
| macOS / iOS / FreeBSD / NetBSD / OpenBSD | 103 バイトまで |
| その他の Unix | 103 バイトまで |

それを超えるパスは、OS ごとに次の順に試し、最初に使えた経路で bind / connect する。どの経路も呼び出し元のプロセスの cwd を変えない。

| OS | 試す順番 |
|---|---|
| Linux / Android (`/proc` を使える) | [/proc](#proc) |
| Linux / Android (`/proc` を使えない) | [相対パス](#相対パス) → [symlink](#symlink) → [スレッド cwd](#スレッド-cwd) |
| FreeBSD | [bindat](#bindat) |
| macOS / iOS | [相対パス](#相対パス) → [広げた sockaddr](#広げた-sockaddr) → [symlink](#symlink) → [スレッド cwd](#スレッド-cwd) |
| NetBSD / OpenBSD / illumos / その他の Unix | [相対パス](#相対パス) → [symlink](#symlink) |

仕様の最後の経路 (fork) は作っていない。Python から fork した子は Python を実行するので、仕様が求める「子の区間は async-signal-safe」を満たせないため ([DR-0001](./decisions/DR-0001-python-port-design.md))。順番の最後まで届かなかったパスは `ENAMETOOLONG` になる。

### 各経路

#### /proc

Linux / Android。ディレクトリを `O_PATH` で開き、`/proc/self/fd/<fd>/<ファイル名>` を標準に渡す。

- ディレクトリ部分の長さに上限は無い。綴り全体が 107 バイトまでなので、ファイル名はおよそ 90 バイトまで
- `/proc/self/fd/<fd>` を引けない時 (`/proc` の無い chroot や最小のコンテナ) と、ファイル名が収まらない時は、`/proc` を使えない時の順に進む

#### bindat

FreeBSD。ctypes で `bindat(2)` / `connectat(2)` を呼び、ディレクトリの fd を基準にファイル名で渡す。ファイル名は 103 バイトまで。ディレクトリを `O_RDONLY` で開くので、読み取り権限も要る。

#### 相対パス

cwd からの相対パスが標準にそのまま渡せる長さなら、それを渡す。元のパスが絶対パスの時だけ使う。文字列を書き直すだけで `chdir` しない。

- パスが cwd (`getcwd`、または cwd と同じディレクトリを指す `$PWD`) の下にあれば前を削る。そうでなければ両方を実パスに揃えて `..` で上る綴りを作る。短い方を使う
- 相対パスが元と同じディレクトリを指すことを確かめてから使い、bind / connect の後に cwd が変わっていないことも確かめる ([後述](#相対パスの経路は他のスレッドの-chdir-に弱い))

#### 広げた sockaddr

macOS / iOS。XNU は `sun_path` が 104 バイトを超えるアドレスも、パスが 253 バイトまでなら受け付ける。ctypes で libc の `bind(2)` / `connect(2)` に手で組んだ sockaddr を渡す。元のパスが 253 バイト以内ならそれを、無理なら相対パスを渡す。

#### symlink

一時ディレクトリに `sockat.XXXXXXXXXX` (乱数 10 文字) の symlink を作り、それを通して bind / connect して、すぐ消す。

| | symlink の先 | 渡す綴り | `/tmp` で届く範囲 |
|---|---|---|---|
| bind | ソケットのディレクトリ | `<置き場>/sockat.XXXXXXXXXX/<ファイル名>` | ファイル名が 84 バイト (`sun_path` が 104 の OS は 80、macOS は 230) 以内 |
| connect | ソケットのファイル | `<置き場>/sockat.XXXXXXXXXX` | ディレクトリの深さにもファイル名の長さにもよらない |

- 置き場の候補は `TMPDIR` (空でない絶対パスの時)、`/tmp` の順。綴りが収まり、安全で、symlink を作れた最初の候補を使う
- **安全な置き場**は次のどちらか。満たさない候補には何も作らず次へ進む
  - sticky bit が立っていて、持ち主が root か自分 (`/tmp` がこれ)
  - 持ち主が自分で、group と other に書き込み権限が無い (macOS の `$TMPDIR` がこれ)

#### スレッド cwd

Linux / Android と macOS / iOS。使い捨ての `threading.Thread` の cwd だけをディレクトリに移し、そのスレッドでファイル名で bind / connect する。

| OS | やり方 | 使えない時 |
|---|---|---|
| Linux / Android | ctypes の `unshare(CLONE_FS)` → `os.fchdir` | `unshare` を断られた時 (docker の既定の seccomp は `EPERM`) |
| macOS / iOS | ctypes の `pthread_fchdir_np` (libSystem の非公開 API) | 関数が無い、または失敗した時 |

- プロセスの cwd も、他のスレッドの cwd も変わらない。connect した相手から見える peer の pid も呼び出し元のまま
- ファイル名は sockaddr に入る長さ (Linux 107、macOS は広げた sockaddr で 253 バイト) まで
- ディレクトリの開き方: Linux は `O_PATH`、macOS は `O_SEARCH` (どちらも検索権限で足りる)

### 経路の失敗の扱い

経路が実際に bind / connect して失敗した時 (`ENOENT`、`EADDRINUSE`、`ECONNREFUSED` 等) は、そのエラーを返して次の経路を試さない。次へ進むのは、その経路自体が使えない時 (長さが足りない、`/proc` を使えない、カーネルが広げた sockaddr を受け付けない、置き場が安全でない・symlink を作れない、スレッドの cwd を移せない) だけ。

### どこから届かなくなるか

標準にそのまま渡せない長さのパスが `ENAMETOOLONG` になる時 (数字はバイト)。

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux (スレッド cwd を使える) | ファイル名 > 107 | ファイル名 > 107、symlink も使えない |
| Linux (スレッド cwd を使えない) | ファイル名が `/proc` の綴りに収まらず、相対パス > 107、ファイル名 > 84 か symlink を使えない | ファイル名が `/proc` の綴りに収まらず、相対パス > 107、symlink も使えない |
| FreeBSD | ファイル名 > 103 | ファイル名 > 103 |
| macOS (スレッド cwd を使える) | ファイル名 > 253 | ファイル名 > 253、symlink も使えない |
| NetBSD / OpenBSD | 相対パス > 103、ファイル名 > 80 か symlink を使えない | 相対パス > 103、symlink も使えない |
| illumos | 相対パス > 107、ファイル名 > 84 か symlink を使えない | 相対パス > 107、symlink も使えない |

- `/proc` を使えない Linux では「`/proc` の綴りに収まらず」の条件が外れる
- symlink を使えないのは、安全で symlink を作れる置き場が無い時 (`TMPDIR` も `/tmp` も使えないサンドボックス等)

## 長いパスで標準と違う点

### カーネルが返すアドレスは元のパスではない

`getsockname()` (listener と accept したソケット) と `getpeername()` (connect したソケット) は、listener を bind した時にカーネルに渡した綴りを返す。

| bind の経路 | 返るもの |
|---|---|
| /proc | `/proc/self/fd/<fd>/<ファイル名>` (fd はもう閉じている) |
| 相対パス | 相対パス |
| 広げた sockaddr | 途中で切れたパス。CPython 3.15 の macOS 版では、切れた後ろに無関係なバイトが付くことがある (標準の `getsockname` の振る舞い。3.13 はちょうど切れる) |
| symlink | もう無い symlink を通したパス |
| スレッド cwd、bindat | ファイル名だけ |

元のパスは自分で持っておき、それを使う。

### 名前で終わらないパスは ValueError

標準にそのまま渡せない長さのパスで、最後がファイル名でないもの (`dir/`、`dir/.`、`dir/..`、`..`) は、何も作らずに `ValueError` を返す。それより短いパスは標準にそのまま渡すので、カーネルのエラーになる。

### NUL を含むパスは ValueError

先頭以外に NUL を含むパスは、長さによらず `ValueError` を返す。標準は NUL で黙って切り詰めて別のパスに bind する (`b"/tmp/a\0b"` が `/tmp/a` になる)。Linux / Android で先頭が NUL のパスは abstract namespace のアドレスなので、長さによらず標準にそのまま渡す。

### ディレクトリは呼ぶ少し前に引かれる

/proc・相対パス・bindat・スレッド cwd の経路は、ディレクトリを先に引いて (開く、または実パスにする) から、ファイル名で bind / connect する。その間に途中のディレクトリが rename されたり symlink が張り替えられたりすると、引いた時点のディレクトリにソケットができる。広げた sockaddr と symlink の経路は標準と同じに振る舞う。

### 相対パスの経路は、他のスレッドの chdir に弱い

sockat 自身は cwd を変えない。bind / connect の後に cwd が変わっていたら、結果を捨てて errno の無い `OSError` を返す (できたかもしれないソケットファイルは、場所が分からないので消さない)。変えて戻した場合は見えない。

### symlink が残ることがある

symlink を作ってから消すまでの間にプロセスが死ぬと、`TMPDIR` か `/tmp` の下に `sockat.XXXXXXXXXX` が残る。

## エラー

| 状況 | エラー |
|---|---|
| ディレクトリが無い / connect 先が無い | `FileNotFoundError` (`ENOENT`) |
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
listener = sockat.bind(path)
server = await asyncio.start_unix_server(handle, sock=listener)

reader, writer = await asyncio.open_unix_connection(sock=sockat.connect_nonblocking(path))
```

`connect_nonblocking` は相手を待たないので、イベントループから直接呼べる。`bind` も相手を待たない (スレッド cwd の経路ではスレッドの終わりを待つが、bind してすぐ終わる)。

## 確かめていないこと

- Android と iOS は動かしていない。Linux / macOS と同じ経路を使う
- NetBSD・OpenBSD・illumos は CI の VM で試験を回している
- ブロックしない connect が backlog の満杯で返すエラーは、Linux と macOS でだけ確かめた
