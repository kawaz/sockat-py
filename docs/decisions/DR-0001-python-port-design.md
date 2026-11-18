# DR-0001: Python 版の作り方 (作る経路、公開 API、標準に揃える点、配布)

- Status: Accepted (fork の経路を作らないことは kawaz 裁定 2026-11-18。NUL は統括の裁定 2026-11-18。他は統括の判断)
- Date: 2026-11-18

## Context

- sockat の振る舞いの正本はハブの [SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)。経路の設計の判断はハブの [DR-0012](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0012-own-directory-and-thread-cwd.md) にある
- Python の標準 (`socket` モジュール) は `len >= sizeof(sun_path)` のパスを errno の無い `OSError('AF_UNIX path too long')` で断る。macOS の CPython 3.11 / 3.13 / 3.14 / 3.15 で 103 バイトまで通り 104 から断ることを確かめた
- SPEC は「その言語で作れない経路は飛ばし、README に OS × 経路の表で示す」「fork の経路を作れない言語は `connect_any` 系を出さない」を認めている

## Decision

### 1. 作る経路

| 経路 | 作り方 | 使う OS |
|---|---|---|
| 中心の経路 (D と R) | [DR-0004](DR-0004-own-directory-route.md) | 手段のある OS |
| スレッド cwd (中心の経路の手段) | sockat が作る使い捨ての `threading.Thread` で、ctypes の `unshare(CLONE_FS)` → `os.fchdir` (Linux。seccomp の filter が掛かっていれば `unshare` を呼ばずに使えないとみなす) / `pthread_fchdir_np` (Apple) をしてから、そのスレッドで `R/<ファイル名>` で bind / connect し、スレッドを捨てる | Linux / Android、Apple の OS |
| bindat / connectat (中心の経路の手段) | ctypes で `bindat(2)` / `connectat(2)` | FreeBSD |
| /proc 版 (中心の経路の手段) | `/proc/self/fd/<D の fd>/R/<ファイル名>` を標準に渡す | Linux / Android |
| /proc (connect の予備) | `os.open(dir, O_PATH \| O_DIRECTORY)` の fd で `/proc/self/fd/<fd>/<ファイル名>` を標準に渡す | Linux / Android |
| fd で掴む形 (connect の予備) | [DR-0002](DR-0002-unsafe-base-held-by-fd.md) | Linux / Android |
| 相対パス (connect の予備) | 文字列を書き直して標準に渡す | FreeBSD 以外で `/proc` を使えない時 |
| 広げた sockaddr (connect の予備) | ctypes で libc の `connect(2)` に手で組んだ sockaddr を渡す | Apple の OS |
| symlink (connect の予備) | `os.symlink` で作り、標準に渡し、消す | FreeBSD 以外 |
| connectat (connect の予備) | ctypes で `connectat(2)` にソケットのディレクトリの fd とファイル名 | FreeBSD |
| fork | 作らない (kawaz 裁定) | — |

- 経路の順番、使える条件、失敗をやり直さない扱い、D・R・symlink の置き場と名前は SPEC のとおり。Python での作り方は [DR-0004](DR-0004-own-directory-route.md)
- 長いパスは最初にソケットのディレクトリを `stat` で引く (無ければ `ENOENT`)。bind が fork の経路の場所に来たら `ENAMETOOLONG` を返す
- スレッド cwd の手段は CPython の 1 スレッド = 1 OS スレッドに頼る。他のスレッドの `os.getcwd()` が変わらないことを macOS と Linux で確かめた
- ctypes を使う呼び出しは `src/sockat/_sys.py` にだけ置き、最初に要った時に import する

理由: Python から `os.fork()` した子は Python を実行するので、SPEC の「子の区間は async-signal-safe」を満たせない。満たすには C 拡張が要り、配布が OS × arch の wheel になる。純 Python で 1 つの wheel にする方を取る (kawaz 裁定)。fork の経路が無いことで bind が届かなくなる所は [DR-0004](DR-0004-own-directory-route.md) の決定 4 (ハブの D12-Q3 で kawaz 裁定待ち)。

### 2. 公開 API

| 関数 | SPEC の操作 | 戻り値 |
|---|---|---|
| `sockat.bind(path)` | `bind` | listen 済みの `socket.socket` |
| `sockat.bind_ex(path)` | 後始末つきの bind | `(listener, discard)` ([DR-0004](DR-0004-own-directory-route.md) の決定 7) |
| `sockat.connect(path)` | `connect` | `socket.socket` (ブロックする) |
| `sockat.connect_nonblocking(path)` | `connect_nonblocking` | 非ブロッキングの `socket.socket` |

- `connect_any` 系は出さない (fork の経路が無いため)
- 長いパスでは、戻り値は `socket.socket` の subclass で、アドレスを元のパスで返す ([DR-0003](DR-0003-report-the-given-path.md))
- `path` は `str` / `bytes` / `os.PathLike`。標準の `socket.bind` は `os.PathLike` を受けない (3.11〜3.15 で確認) が、Python のファイル API の慣習に合わせて受け、`os.fspath` した値を標準に渡す
- 経路 (`_route.Route`) は公開しない。試験はパッケージの中から各経路の関数を呼ぶ。環境を読む部分 (D の置き場、symlink の置き場の候補、macOS で広げた sockaddr を試すか、`/proc/self/fd` の場所、スレッド cwd を試すか) は `_route.Config` にして試験から差し替える

### 3. 標準に揃える点

| 点 | 決定 |
|---|---|
| 標準にそのまま渡す長さ | `sun_path` − 1。Linux / Android / illumos は 107、Apple と BSD は 104 − 1 = 103。他の OS は 103 (小さい方に倒す) |
| ソケットの作り方 | どの経路も `socket.socket(AF_UNIX, SOCK_STREAM)` で作る (close-on-exec、既定の timeout、`SO_NOSIGPIPE` を付けない点まで標準と同じ) |
| listen | 引数なしの `listen()` (標準の既定の backlog) |
| ブロックしない connect | connect の前に `setblocking(False)` |
| ctypes の connect の `EINTR` | 標準と同じく、ブロックするソケットは書き込み可能になるまで待って `SO_ERROR` を読み、非ブロッキングは `InterruptedError` |

### 4. NUL を含むパス

- 先頭以外に NUL を含むパスは、長さによらず `ValueError`。標準は NUL で黙って切り詰めて別の場所に bind する (`b"/tmp/a\0b"` が `/tmp/a` になる。Linux / macOS で確認)
- Linux / Android で先頭が NUL のパスは abstract namespace のアドレスで、pathname ではないので対象外。長さによらず標準にそのまま渡す (標準で abstract socket を使うコードが置き換えで壊れないように)

### 5. エラーの型

| SPEC の状況 | Python の型 |
|---|---|
| OS のエラー | errno 付きの `OSError` (`FileNotFoundError` 等のサブクラスになる) |
| 引数の誤り (名前で終わらない長いパス、先頭以外に NUL を含むパス) | `ValueError` (Python の引数の値の誤りの慣習) |
| 一般のエラー (相対パスの経路の途中で cwd が変わった) | errno の無い `OSError` (標準の `AF_UNIX path too long` と同じ形) |

### 6. 対応する版と道具

| 項目 | 決定 |
|---|---|
| Python | 3.11 以上 (3.10 は 2026-10 にサポート終了) |
| 中身 | 純 Python (ctypes は標準ライブラリ)。依存なし |
| ビルド | uv、build backend は `uv_build` |
| 型と lint | mypy strict (Linux / macOS / FreeBSD の platform ごとに通す)、ruff |
| 試験 | pytest。macOS と Linux (docker で /proc あり・なし) |

## Alternatives Considered

- 案 A: Python から `os.fork()` して子に `fchdir` → bind / connect させる
  - 不採用理由: 子が Python を実行し、`os.register_at_fork` で登録された処理も走る。async-signal-safe にならない。3.12 以降はスレッドのあるプロセスで fork すると DeprecationWarning も出る
- 案 B: `sys.executable` を fork+exec し、渡した fd に子の Python が bind / connect する (`_posixsubprocess` の子の区間は C)
  - 不採用理由: SPEC の fork の経路と仕組みが違う。PyInstaller 等で固めたアプリでは `sys.executable` がアプリ本体を指し、それを起こしてしまう
- 案 C: PyO3 で sockat-rs を包む
  - 不採用理由: Rust 標準のソケット設定 (`SO_NOSIGPIPE`、backlog) になり Python 標準と揃わない。経路を Python 側の試験から強制できない。配布が OS × arch の wheel になる
- 案 D: C 拡張を任意にし、入っている時だけ fork の経路と `connect_any` 系を出す
  - 不採用理由: 入れ方で公開 API が変わる
- 案 E: 標準にそのまま渡す長さを、import 時に標準で試して求める
  - 不採用理由: import のたびに試す手間がかかる。値は OS で決まるので表で足り、試験が標準自身で境界を確かめる

## Consequences

- fork の経路でしか届かない所は、bind も connect も `ENAMETOOLONG` になる。bind では中心の経路が使えない所全部 (NetBSD / OpenBSD / illumos の長いパス全部を含む。[DR-0004](DR-0004-own-directory-route.md) の決定 4)。README の OS × 経路の表に「fork の経路なし」と書く
- `connect_any` 系が欲しくなったら、他の言語の実装と同じく使う人が選んで有効にする別のもの (ハブの DR-0007。例: C 拡張を含む別の配布物が `sockat.any` を足す) として出す。既定の `sockat` は純 Python のまま変えない (kawaz 裁定 2026-11-18)
- SPEC が変わったら、この実装と README / MANUAL を追従させる

## 関連

- [sockat の SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)
- [ハブの DR-0012](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0012-own-directory-and-thread-cwd.md)
- [DR-0004](DR-0004-own-directory-route.md)
- [他言語への移植の調査](https://github.com/kawaz/sockat/blob/main/docs/research/2026-11-18-ports-to-other-languages.md)
