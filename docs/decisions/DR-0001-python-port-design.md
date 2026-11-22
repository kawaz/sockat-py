# DR-0001: Python 版の作り方 (作る経路、置き場 B・D・E、公開 API、標準に揃える点、配布)

- Status: Accepted (fork の経路を作らないことと `sockat.ex` の置き場は kawaz 裁定。経路・置き場・綴りの性質・関数の組はハブの DR-0017 の kawaz 裁定と統括の決定 (sockat-py の問い PY-Q1〜Q11 への答えを含む)。Python での形は統括の判断 2026-11-21)
- Date: 2026-11-21

## Context

- sockat の振る舞いの正本はハブの [SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md) と [SPEC-routes](https://github.com/kawaz/sockat/blob/main/docs/SPEC-routes-ja.md)。今の設計の判断はハブの [DR-0017](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0017-resolve-through-persistent-base.md) (長いパスは置き場 B の中の消さない symlink を通る短い綴りに resolve する。既定の bind も綴りを残す。fork はしない)
- Python の標準 (`socket` モジュール) は `len >= sizeof(sun_path)` のパスを errno の無い `OSError('AF_UNIX path too long')` で断る。macOS の CPython 3.11 / 3.13 / 3.14 / 3.15 で 103 バイトまで通り 104 から断ることを確かめた
- Python から呼べないシステムコール (`bindat` / `connectat`、`unshare`、`pthread_fchdir_np`、広げた sockaddr の `bind` / `connect`) は ctypes で呼べる。`flock`、`mkdirat` / `symlinkat` / `openat` (`dir_fd=`)、`fchmod` は標準ライブラリにある

## Decision

### 1. 作る経路

| 経路 | 作り方 | 使う OS |
|---|---|---|
| link-dir | B・D の中に `os.symlink(..., dir_fd=D)` で E を作り、`B/D/E/<ファイル名>` を標準に渡す。B の綴りは procB (`/proc/<pid>/fd/<B の fd>`) か B の実パス | 全部 |
| link-file | E をソケットのファイルを指して作り、bind は `B/D/E`、既定の connect は B の直下の乱数の E を `B/E` で標準に渡してすぐ消す | bind は辿ると実測した OS と未確認の OS、connect は全部 |
| thread-cwd | sockat が作る使い捨ての `threading.Thread` で ctypes の `unshare(CLONE_FS)` → `os.fchdir` (Linux。seccomp の filter が掛かっていれば `unshare` を呼ばずに使えないとみなす) / `pthread_fchdir_np` (Apple) をし、そのスレッドで `E/<ファイル名>` を bind する | Linux / Android、Apple の OS |
| at | ctypes で `bindat(2)` に D の fd | FreeBSD |
| proc-direct | `os.open(dir, O_PATH \| O_DIRECTORY)` の fd で `/proc/self/fd/<fd>/<ファイル名>` を標準に渡す | Linux / Android |
| relative | 文字列を書き直して標準に渡す | `/proc` を使えない Linux、Apple、FreeBSD 以外 |
| widened | ctypes で libc の `connect(2)` に手で組んだ sockaddr を渡す | Apple の OS |
| at-direct | ctypes で `connectat(2)` にソケットのディレクトリの fd とファイル名 | FreeBSD |
| thread-cwd-direct | thread-cwd と同じスレッドの cwd をソケットのディレクトリに移し、ファイル名で connect する | Linux / Android、Apple の OS |

- 経路の順番、使える条件、失敗をやり直さない扱いは SPEC のとおり。fork の経路は作らない (SPEC も作らない)
- thread-cwd は CPython の 1 スレッド = 1 OS スレッドに頼る。使い捨てのスレッドは自分の fd (ディレクトリの fd とソケットの `dup`) で動き、終わる時に自分で閉じる。呼び出し元はスレッドが終わるまで待ち、`start` の中や `join` が割り込まれても (`KeyboardInterrupt`) スレッドが終わってから割り込みを投げ直す。呼び出し元が自分の fd を閉じても、スレッドが番号の使い回された別のディレクトリに移ることは無い
- ctypes を使う呼び出しは `src/sockat/_sys.py` にだけ置き、最初に要った時に import する

### 2. 置き場 B・D・E の Python での作り方

| 点 | 決定 |
|---|---|
| B の置き場 | `/tmp` → `$XDG_RUNTIME_DIR` → `$TMPDIR` → ソケットのディレクトリ。環境変数は空・相対パスなら使わない。realpath の `(st_dev, st_ino)` が同じ置き場 (`$TMPDIR=/tmp` 等) は 1 回だけ試す。1 つのプロセスが使う B は置き場 1〜3 で 1 つ (既定の connect が初めて作った B も以後使う。`usable_cross_uid` の B と同じ置き場なら同じ B)、ソケットのディレクトリの B はディレクトリごと。作れなかった置き場は覚えず、次の呼び出しで試し直す |
| 持ち主の uid | 実効 uid (`os.geteuid()`)。作った物の持ち主で、`sockat-<uid>` の uid |
| 消された B・D | 持っている fd の `st_nlink` が 0 か、名前で引いた `(st_dev, st_ino)` が fd と違えば、次の呼び出しで外して作り直す (APFS では消したディレクトリの `st_nlink` が 2 のままなので名前の確かめを省かない)。外した B・D の fd は閉じずにプロセスの間持ち続ける (閉じると番号が使い回され、procB の綴りの生きているソケットが別のディレクトリに当たる。閉じるのは fork の子だけ) |
| skipped の列 | その OS の行に並ぶ経路だけ。手段の無い経路 (NetBSD・OpenBSD・illumos の thread-cwd、Linux / Apple 以外の thread-cwd-direct) は入れない |
| 置き場のパス | realpath にしてから、ルートからの途中 (どれも持ち主が root か自分で、かつ他人が書けないか sticky) を確かめ、`O_DIRECTORY \| O_NOFOLLOW` で開いて `mkdirat` する。実パスの綴りもこの realpath から作る |
| B を作る時 | B を使う経路に来た時だけ。作らずに分かる外れ (置き場が無い、途中の確かめに外れる、`usable_cross_uid` の時に other の x が無い) を飛ばした最初の置き場の実パスで綴りの長さを見積もり、収まらなければ作らず、次の置き場にも移らない (B を使う経路を飛ばす)。見積もりに通った後に作れなければ次の置き場へ進み、その置き場でも見積もり直す (収まらなければ作らずそこで止まる)。普段の B と `usable_cross_uid` の B は別の枠に持ち (普段の bind が `usable_cross_uid` の B に乗り換えない)、同じ置き場なら同じ B と D |
| B と D の作り方 | `mkdirat` 0700 の直後に名前で `os.chmod(..., 0o711, dir_fd=親)` (umask で削られた分を戻す)、それから `O_DIRECTORY \| O_NOFOLLOW` で開いて `fstat` で実ディレクトリ・持ち主を確かめる。外れた・開けない時は作った空のディレクトリを消す。名前で chmod するのは Linux の `fchmodat` が `AT_SYMLINK_NOFOLLOW` を受け付けないためで、`mkdirat` が通った後の名前は他人に消せず差し替えられないので、開いた後の `fstat` で足りる |
| 既にある B | `O_NOFOLLOW` で開いて実ディレクトリ・持ち主が自分・他人 (group を含む) が書けないことを確かめ、0711 でなくても mode を変えずに使う。外れたら (占拠)、自動の置き場はその置き場を使えないとして次の置き場へ進み、B を使う経路の `reason` に占拠された B のパスを書く。`Options.base` は symlink なら `EPERM`、ディレクトリでも symlink でもなければ `ENOTDIR` (OS ごとに違う `O_NOFOLLOW` の errno でなく `lstat` で見分ける) |
| 既定の bind の link-dir の E | (D, ソケットのディレクトリの realpath) ごとに 1 つを D の中の表に持って使い回す。使う前に D の fd から `readlinkat` し、消えたか指す先が違えば外して作り直す。表の更新 (作る・外して作り直す) だけを D の lock で守り、bind の区間は守らない。bind に失敗しても、その呼び出しで作った E も消さない (並行する他の既定の bind が使い回しうる。残っても次の bind が使う)。`_ex`・resolve・link-file は呼び出しごとに作り、失敗で消す |
| `Options.base` の確かめ | 絶対パスで名前で終わらなければ `ValueError`。長いパスでは、ソケットのディレクトリを引いた後・経路を試す前に、bind でも connect でも何も作らずに確かめる (途中と、あれば B 自身)。安全の確かめは呼び出しのたびに行い、other の x の判定 (`usable_cross_uid`) は B を初めて持った時点の物を覚えて見直さない |
| `/proc` を使えるか | 使う所ごとに、stat した `(st_dev, st_ino)` が fd の `fstat` と同じ時だけ。connect の行は `/` を開いた fd で `/proc/self/fd/<fd>`、proc-direct はそのソケットのディレクトリの fd で `/proc/self/fd/<fd>`、procB は B の fd で `/proc/<pid>/fd/<fd>` (B を開く前の見積もりは `/` を開いた fd で同じく) |
| `usable_cross_uid` | bind 系だけが見る。途中と B 自身が other に x を持つ B だけを使う |
| `usable_with_private_tmp` | procB はいつも真。B の実パスは、置き場の realpath する前のパスと B の実パスのどちらかが `/tmp`・`/var/tmp` (とその realpath) を通れば偽 |
| 診断の `base` | 一時の置き場の B は綴りの形 (`proc`・`tmp`・`runtime`・`tmpdir`)、ソケットのディレクトリと `Options.base` の B は `sockdir`・`given` |
| D | 乱数 10 文字の名前で上の手順で作り、`fcntl.flock(LOCK_SH)` を掛けた fd を持ち続ける。作れない時はその呼び出しの間 B ごとに覚えて D を使う経路を使わず (別の B は探さない)、次の呼び出しで作り直す |
| sweep | B と D の中は fd を基準に列挙し (`os.scandir(fd)`)、`flock(LOCK_EX \| LOCK_NB)` を取った後に名前の `(st_dev, st_ino)` がロックした fd と同じかを確かめてから消す。名前が D の形の物だけを見て、symlink でない物がある D は残す。`sweep()` は確かめに外れた置き場と読めない B を黙って飛ばす |
| D と `sweep` の競合 | flock を掛けた後に名前を引き直し、`(st_dev, st_ino)` が開いた物と違えば (他のプロセスの `sweep` が、ロックの掛かる前の D を消した) 別の名前で作り直す |
| E の名前 | resolve の E は D ごとのカウンタ (36 進、0 から)。thread-cwd・at と B の直下の E は乱数 10 文字。綴りは E が受け取る名前の長さで、E を作る前に見積もる |
| プロセスの状態 | B (と fd、D) はモジュールの `State` に持つ。`os.register_at_fork(after_in_child=...)` で、fork の子では親の B・D の fd を閉じて状態を作り直す (子は親の D を使わず自分の D を作る) |
| 環境を読む部分 | 置き場の候補、uid、`/proc` の場所、thread-cwd を試すか、widened を試すか、link-file を試すか・どう見分けるかを `_place.Config` にし、試験から差し替える |

### 3. 公開 API

| 置き場 | 関数・型 |
|---|---|
| `sockat` | `bind(path)`・`connect(path)`・`connect_nonblocking(path)` (`socket.socket` を返す) |
| `sockat.datagram` | `bind(path)`・`unbound()`・`connect(sock, path)` |
| `sockat.ex` | `bind`・`connect`・`connect_nonblocking` (各 `(path, *, options=None)`。`options` はキーワードでだけ渡せる)、`resolve_for_bind`・`resolve_for_connect`、`sweep()`・`sweep_base(path)`、型 `Options`・`Bound`・`Connected`・`Report`・`ForBind`・`ForConnect`・`SocketFile`・`Skipped`・`Spelling` (`Original`・`CwdRelative`・`Linked`・`Dangling`)・`Until` |
| `sockat.ex.datagram` | `bind(path, *, options=None)`・`connect(sock, path, *, options=None)` |

- `sockat.ex` は `import sockat` では読み込まない (標準の `socket.connect_ex` と紛れるため、使う人が `import sockat.ex` で選ぶ)。`sockat.datagram` は読み込む
- `Options` はキーワード専用の frozen dataclass (`usable_cross_uid: bool = False`、`base: str | bytes | os.PathLike | None = None`)
- `Bound(socket, file, spelling, route, base, skipped)`・`Connected(socket, route, skipped)`・`Report(route, skipped)` は NamedTuple (分解して受け取れる)。`ForBind(path, spelling, route, base, skipped)` / `ForConnect(path, spelling, route, skipped)` は frozen dataclass と `cleanup()`。どちらも `remove()` を持たず、`ForBind` は bind の後に呼ぶ `socket_file()` で `SocketFile` を返す (綴りを辿って realpath と `(st_dev, st_ino)` を覚える。辿った先がソケットでなければ `ENOTSOCK`。`CwdRelative` の綴りは resolve の時と cwd を比べる)
- `Spelling` は基底のクラスと、値ごとの frozen dataclass。`match bound.spelling: case sockat.ex.Linked(removable=True): ...` で分岐できる。`Until` は `StrEnum` (`"Process"` / `"Cleanup"`)
- `SocketFile` は `remove()` と `cleanup()` を持つ (SPEC の Python の形「戻り値に引数なしの関数を含める」を、引数なしのメソッドで満たす)
- `sockat.ex.bind` が bind の直後に覚える場所: link-dir・thread-cwd・at は、経路を組む前に `os.path.realpath` で引いたソケットのディレクトリ (E が指す先) とファイル名をそのまま使い、元のパスを引き直さない。standard は標準に渡した綴り (元のパスか絶対パスの候補)、link-file は E が指す先 (相対パスなら `os.getcwdb()` を前に付けた物) を、bind の後に `os.path.realpath` する。どちらも最後の要素が symlink なら辿った先を覚え、`lstat` してソケットでなければ `ENOTSOCK`。cwd を前後で比べるのは、相対パスのまま標準に渡した (`CwdRelative`) 時だけ
- resolve の `path` は、渡したパスが `str` なら `str`、`bytes` なら `bytes`
- 長いパスでは、戻り値は `socket.socket` の subclass で、アドレスを元のパスで返す ([DR-0003](DR-0003-report-the-given-path.md))。長い絶対パスを実パスの候補で標準に渡した時も同じ subclass で元のパスを返す。相対パスの bind が経路に進んだ時は、相対の文字列でなく実パスの候補の形を返す。`sockat.datagram.unbound()` も同じ subclass で、長いパスに connect すると `getpeername()` が元のパスを返す
- `path` は `str` / `bytes` / `os.PathLike`。入口で 1 度だけ `os.fspath` し、その値を覚える・bind する・返すのすべてに使う
- 経路は公開しない (`sockat.ex` が経路の名前の文字列を診断用に返すだけ)。試験はパッケージの中から各経路の関数を呼び、connect は試す順を渡して強制する

### 4. 標準に揃える点

| 点 | 決定 |
|---|---|
| 標準にそのまま渡す長さ | `sun_path` − 1。Linux / Android / illumos は 107、Apple と BSD は 103。他の OS は 103 (小さい方に倒す) |
| ソケットの作り方 | どの経路も `socket.socket(AF_UNIX, SOCK_STREAM)` (DGRAM は `SOCK_DGRAM`) で作る (close-on-exec、既定の timeout、`SO_NOSIGPIPE` を付けない点まで標準と同じ) |
| listen | 引数なしの `listen()` (標準の既定の backlog) |
| ブロックしない connect | connect の前に `setblocking(False)` |
| ctypes の connect の `EINTR` | 標準と同じく、ブロックするソケットは書き込み可能になるまで待って `SO_ERROR` を読み、非ブロッキングは `InterruptedError` |
| DGRAM の connect | 渡されたソケットで経路を試す。失敗してもそのソケットは閉じない |

### 5. NUL を含むパス

- 先頭以外に NUL を含むパスは、長さによらず `ValueError`。標準は NUL で黙って切り詰めて別の場所に bind する (`b"/tmp/a\0b"` が `/tmp/a` になる。Linux / macOS で確認)
- Linux / Android で先頭が NUL のパスは abstract namespace のアドレスで、長さによらず標準にそのまま渡す。abstract namespace の無い OS では先頭の NUL も `ValueError`

### 6. エラーの型

| SPEC の状況 | Python の型 |
|---|---|
| OS のエラー | errno 付きの `OSError` (`FileNotFoundError` 等のサブクラスになる) |
| 引数の誤り (名前で終わらない長いパス、先頭以外に NUL を含むパス) | `ValueError` |
| 一般のエラー (relative の途中で cwd が変わった、相対パスのまま標準に渡した `sockat.ex.bind` で覚える前に cwd が変わった) | errno の無い `OSError` |
| `_ex` と resolve の失敗 | 既定の呼び出しと同じ例外に、属性 `skipped` (`Skipped` のタプル) と `route` (失敗した経路の名前か `None`) を足す |

### 7. bind と resolve の相対パス

ハブ SPEC の「相対パスを絶対パスにする」(ハブ DR-0019) の Python での形。

- 候補は `src/sockat/_absolute.py` で作る。つなぐ・畳む・比べるは `bytes` で行い、`str` で渡されたパスも `os.fsencode` した物で扱う (返す綴りは渡した型に戻す)
- 実パスの候補は、ソケットのディレクトリを `os.stat` で引いてディレクトリと確かめてから `os.path.realpath(..., strict=True)` する。`os.path.abspath` で先に絶対にしない (`a/sym/..` を字句で畳み、別のディレクトリを引く)
- cwd は `os.getcwd` / `os.getcwdb` で読まず、`_sys.getcwd` で読み、PATH_MAX (`os.pathconf("/", "PC_PATH_MAX")`、取れなければ 4096) を上限にする。PATH_MAX バイト以上の cwd は、どの OS もここで「引けなかった」(`ENAMETOOLONG`、illumos は `ERANGE`) になり、手順 5 に進む。illumos は `getcwd(3C)` を PATH_MAX のバッファで 1 回だけ呼び、他の OS は `os.getcwdb` の結果の長さで打ち切る。相対パスの realpath は、その cwd を前に付けた絶対パスを `os.path.realpath` する (`os.path.realpath` に相対パスを渡すと中で `os.getcwd` を呼ぶ)。CPython の `os.getcwd` は `ERANGE` の間バッファを 1024 バイトずつ増やして呼び直すので、illumos の深い cwd では返らず `MemoryError` で落ちた (0.4.0 の CI で 13 分。illumos の手元の実機は無く、CPython のソースとログから判断した)
- `$PWD` の候補は、呼び出しのたびに `os.environb` から読む。字句で畳むのは `_path.fold` (`os.path.normpath` と同じ畳み方) で、畳んだディレクトリの `(st_dev, st_ino)` を元の相対パスのディレクトリと比べてから使う
- ソケットのディレクトリの realpath が `ENAMETOOLONG`・`ERANGE` の時と、realpath が PATH_MAX を超える実パスを返してそれを `os.stat` すると `ENAMETOOLONG` の時 (cwd が深い。`os.path.realpath` と `os.getcwdb` は macOS でも Linux でも失敗せず長いパスを返す。実測 2026-11-22) は、経路を試さず 5 (相対のまま標準へ) に進む。この時 `sockat.ex.bind` は bind したファイルの実パスを `lstat` できず覚えられないので、listener を閉じ、ファイルを残して `ENAMETOOLONG` を返す (届くのは既定の bind だけ)
- 候補を標準に渡した時の戻り値は素の `socket.socket` (カーネルが返す綴りがその候補で、絶対にした形)。長い絶対パスを実パスの候補で渡した時は、元のパスを返す subclass

### 8. 対応する版と道具

| 項目 | 決定 |
|---|---|
| Python | 3.11 以上 |
| 中身 | 純 Python (ctypes は標準ライブラリ)。依存なし |
| ビルド | uv、build backend は `uv_build` |
| 型と lint | mypy strict (darwin / linux / freebsd / netbsd の platform ごとに通す)、ruff |
| 試験 | pytest。macOS と Linux (docker で Python 3.11 / 最新 × seccomp の既定 / unconfined × root / nobody)。ハブの適合試験の CLI は `conformance/cli.py` |

## Alternatives Considered

- 案 A: Python から `os.fork()` して子に `fchdir` → bind / connect させる
  - 不採用理由: SPEC が fork をしない。子が Python を実行し、`os.register_at_fork` で登録された処理も走る
- 案 B: PyO3 で sockat-rs を包む
  - 不採用理由: Rust 標準のソケット設定 (`SO_NOSIGPIPE`、backlog) になり Python 標準と揃わない。経路を Python 側の試験から強制できない。配布が OS × arch の wheel になる
- 案 C: `Spelling` を 1 つのクラスに `kind` と省略できる属性を持たせる
  - 不採用理由: `Linked` 以外で属性が意味を持たない組み合わせを許す。値ごとのクラスなら `match` のクラスパターンで属性ごと分岐できる
- 案 D: `_ex` の失敗を専用の例外型で包む
  - 不採用理由: 呼び出し元が見ている型 (`FileNotFoundError` 等) と `errno` が変わる
- 案 E: D の名前をプロセスの中のカウンタにする
  - 不採用理由: プロセスが終わった後に同じ PID・同じ fd 番号の別のプロセスが現れると、procB の綴りが同じになる (ハブ DR-0017 の案 G)
- 案 F: fork の子でも親の B・D をそのまま使う
  - 不採用理由: 子の procB の綴りは子の pid で綴るが、親の fd を引き継いだ番号が子で閉じられ使い回されうる。親が先に終わると子の持つ複製で親の D のロックが残り続ける
- 案 G: 標準にそのまま渡す長さを、import 時に標準で試して求める
  - 不採用理由: import のたびに試す手間がかかる。値は OS で決まるので表で足り、試験が標準自身で境界を確かめる

## Consequences

- 長いパスの bind は、どの OS でも B の実パスの link-dir で届く所まで届く (`/tmp` の B ならファイル名およそ 77 バイトまで)。それを超えるファイル名は link-file (macOS・NetBSD) と thread-cwd・at (Linux・Apple・FreeBSD) で届き、届かない所は `ENAMETOOLONG`
- 長いパスの既定の bind はプロセスの D に E を残す。試験は共有の `/tmp/sockat-<uid>` を使わない: 経路の試験は試験のディレクトリを置き場にし、公開の呼び出しの試験 (子プロセスを含む) は環境を読む部分を差し替えてセッション専用の置き場を使う
- ソケットのディレクトリの B は `sweep` の対象にならず、残りうる (仕様の「置き場に残る物」)
- SPEC が変わったら、この実装と README・利用マニュアル・リファレンスを追従させる

## 未決

- DGRAM の `send_to` と SEQPACKET は作らない (SPEC で未確定)

## 関連

- [ハブの SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)、[SPEC-routes](https://github.com/kawaz/sockat/blob/main/docs/SPEC-routes-ja.md)
- [ハブの DR-0017](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0017-resolve-through-persistent-base.md)
- [DR-0003](DR-0003-report-the-given-path.md)
