# DR-0001: Python 版の作り方 (作る経路、公開 API、標準に揃える点、配布)

- Status: Accepted (決定 1 の fork の行だけ [PY-Q1](../QUESTIONS.md) の裁定待ち。他は統括の判断、NUL は統括の裁定 2026-11-18)
- Date: 2026-11-18

## Context

- sockat の振る舞いの正本はハブの [SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)。経路の設計の判断は [sockat-rs の DR-0001](https://github.com/kawaz/sockat-rs/blob/main/docs/decisions/DR-0001-long-socket-path-routes.md) にある
- Python の標準 (`socket` モジュール) は `len >= sizeof(sun_path)` のパスを errno の無い `OSError('AF_UNIX path too long')` で断る。macOS の CPython 3.11 / 3.13 / 3.14 / 3.15 で 103 バイトまで通り 104 から断ることを確かめた
- SPEC は「その言語で作れない経路は飛ばし、README に OS × 経路の表で示す」「fork の経路を作れない言語は `connect_any` 系を出さない」を認めている

## Decision

### 1. 作る経路

| 経路 | 作り方 | 使う OS |
|---|---|---|
| /proc | `os.open(dir, O_PATH \| O_DIRECTORY)` の fd で `/proc/self/fd/<fd>/<ファイル名>` を標準に渡す | Linux / Android |
| bindat | ctypes で `bindat(2)` / `connectat(2)` | FreeBSD |
| 相対パス | 文字列を書き直して標準に渡す | FreeBSD 以外 |
| 広げた sockaddr | ctypes で libc の `bind(2)` / `connect(2)` に手で組んだ sockaddr を渡す | macOS / iOS |
| symlink | `os.symlink` で作り、標準に渡し、消す | FreeBSD 以外 |
| スレッド cwd | sockat が作る使い捨ての `threading.Thread` で、ctypes の `unshare(CLONE_FS)` → `os.fchdir` (Linux。seccomp の filter が掛かっていれば `unshare` を呼ばずに使えないとみなす) / `pthread_fchdir_np` (Apple) をしてから、そのスレッドでファイル名で bind / connect し、スレッドを捨てる | Linux / Android、Apple の OS |
| fork | 作らない (PY-Q1 の推奨 a) | — |

- 経路の順番、使える条件、失敗をやり直さない扱い、symlink の置き場と名前は SPEC のとおり
- posix の順の最後 (スレッド cwd の前) でディレクトリを開く。開けなければその errno (無ければ `ENOENT`)。fork の経路の場所に来たら `ENAMETOOLONG` を返す
- スレッド cwd の経路は CPython の 1 スレッド = 1 OS スレッドに頼る。他のスレッドの `os.getcwd()` が変わらないことを macOS と Linux で確かめた
- ctypes を使う呼び出しは `src/sockat/_sys.py` にだけ置き、最初に要った時に import する (Linux は ctypes を読み込まない)

理由: Python から `os.fork()` した子は Python を実行するので、SPEC の「子の区間は async-signal-safe」を満たせない。満たすには C 拡張が要り、配布が OS × arch の wheel になる (PY-Q1)。

### 2. 公開 API

| 関数 | SPEC の操作 | 戻り値 |
|---|---|---|
| `sockat.bind(path)` | `bind` | listen 済みの `socket.socket` |
| `sockat.connect(path)` | `connect` | `socket.socket` (ブロックする) |
| `sockat.connect_nonblocking(path)` | `connect_nonblocking` | 非ブロッキングの `socket.socket` |

- `connect_any` 系は出さない (fork の経路が無いため)
- `path` は `str` / `bytes` / `os.PathLike`。標準の `socket.bind` は `os.PathLike` を受けない (3.11〜3.15 で確認) が、Python のファイル API の慣習に合わせて受け、`os.fspath` した値を標準に渡す
- 経路 (`_route.Route`) は公開しない。試験はパッケージの中から各経路の関数を呼ぶ。環境を読む部分 (symlink の置き場の候補、macOS で広げた sockaddr を試すか、`/proc/self/fd` の場所、スレッド cwd の経路を試すか) は `_route.Config` にして試験から差し替える

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

- fork の経路でしか届かない所は、bind も connect も `ENAMETOOLONG` になる。そうなるのは、スレッド cwd の経路が使えず (NetBSD / OpenBSD / illumos、seccomp の filter の下の /proc の無い Linux)、bind でファイル名が symlink の綴りに収まらないか、symlink を置ける場所が無い時。README の OS × 経路の表に「fork の経路なし」と書く
- PY-Q1 で C 拡張を入れることになったら、決定 1 の fork の行と決定 2 の `connect_any` 系、決定 6 の配布を書き換える。経路の本体は Python のまま。`connect_any` 系は既定で import されない別のモジュール (例: `sockat.any`) に置く (ハブの DR-0007: `connect_any` 系は使う人が選んで有効にする仕組みの後ろに置く)
- SPEC が変わったら、この実装と README / MANUAL を追従させる

## 関連

- [sockat の SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)
- [sockat-rs の DR-0001](https://github.com/kawaz/sockat-rs/blob/main/docs/decisions/DR-0001-long-socket-path-routes.md)
- [他言語への移植の調査](https://github.com/kawaz/sockat/blob/main/docs/research/2026-11-18-ports-to-other-languages.md)
