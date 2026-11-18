# DR-0004: 中心の経路 (自分専用のディレクトリ D と R) の Python での作り方、bind_ex

- Status: Accepted (ハブの DR-0012 の Python での作り方。統括の判断 2026-11-19。中心の経路が使えない時の bind はハブの D12-Q3、`bind_ex` の形はハブの D12-Q4 で kawaz 裁定待ち)
- Date: 2026-11-19

## Context

- ハブの [DR-0012](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0012-own-directory-and-thread-cwd.md) が、長いパスは自分専用のディレクトリ D を fd で掴み、その中の乱数の symlink R を通して `R/<ファイル名>` で bind / connect する形にした。bind は /proc・相対パス・広げた sockaddr・置き場の symlink を使わず、connect はそれらを予備の経路にする
- SPEC の bind は中心の経路が使えない時に fork の経路へ進むが、Python 版は fork の経路を作らない ([DR-0001](DR-0001-python-port-design.md))
- SPEC は後始末つきの bind (`_ex`) を足した

## Decision

### 1. 試す順番

| 順 | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| 1 | `sun_path` − 1 以下なら標準にそのまま (環境も読まない) | 同じ |
| 2 | ソケットのディレクトリを `os.stat` で引く (無ければ `ENOENT`、ディレクトリでなければ `ENOTDIR`)。fd が要る経路のために開く | 同じ |
| 3 | 中心の経路 (手段は決定 3 の順) | 同じ |
| 4 | `ENAMETOOLONG` (決定 4) | 予備の経路 (決定 5) |

- ソケットのディレクトリの fd が要るのは、ソケットのディレクトリに作る D と、connect の予備の /proc・fd で掴む形の `/proc` の確かめ・connectat。開けなければ (`O_RDONLY` で開く OS で読み取り権限が無い等) それらを使えないとして他を試す
- ディレクトリ (ソケットのディレクトリ、D の置き場) の開き方: Linux / Android は `O_PATH | O_DIRECTORY`、Apple の OS は `O_SEARCH` (`EISDIR` / `EINVAL` なら `O_RDONLY`)、他は `O_RDONLY | O_DIRECTORY` (読み取り権限が要る)
- 入口は `_route.run`。長いパスは `routes` が `central_route` を試し、使えなければ `bind_without_central` か `connect_fallbacks` に進む

### 2. D と R

| 手順 | Python での作り方 |
|---|---|
| 置き場を開く | 決定 1 の開き方。ソケットのディレクトリは決定 1 の 2 で開いた fd を使う (開けなかったらこの置き場は使えない) |
| D を作る | `os.mkdir(<名前>, 0o700, dir_fd=<置き場>)`。名前は `sockat.` (ソケットのディレクトリでは `.sockat.`) + `secrets` の英数 62 種 10 文字。`FileExistsError` で引き直し、16 回で打ち切る |
| D を開く | 下の表 |
| 確かめる | `os.fstat` で、ディレクトリ・`st_uid == os.geteuid()`・`mode & 0o077 == 0` |
| 0700 にそろえる | 下の表 |
| R を作る | `os.symlink(<ソケットのディレクトリの realpath>, <R>, dir_fd=<D>)`。R は英数 62 種 10 文字。`FileExistsError` で引き直し、16 回で打ち切る |
| 片付け | `os.unlink(<R>, dir_fd=<D>)`。D は確かめが通り、名前を `os.stat(..., follow_symlinks=False)` した `(st_dev, st_ino)` が fd と一致する時だけ `os.rmdir`。fd を閉じる。成功でも失敗でもし、消す失敗は無視する |

| OS | D の開き方 | 0700 にそろえる |
|---|---|---|
| Linux / Android | `O_PATH \| O_DIRECTORY \| O_NOFOLLOW` | `/proc` を使えれば `os.chmod("/proc/self/fd/<fd>", 0o700)`。使えなければ名前で `O_RDONLY \| O_DIRECTORY \| O_NOFOLLOW` で開き直し、`(st_dev, st_ino)` が同じ時だけ `os.fchmod` (違えば `ESTALE`) |
| Apple の OS | `O_SEARCH \| O_DIRECTORY \| O_NOFOLLOW` (`EISDIR` / `EINVAL` なら `O_RDONLY`) | `os.fchmod` |
| 他 | `O_RDONLY \| O_DIRECTORY \| O_NOFOLLOW` | `os.fchmod` |

- 開けない D と確かめに落ちた D は他人の物かもしれないので、中身も含めて触らない。確かめが通った後に 0700 にそろえられない・R を作れない D は消して、次の置き場へ進む
- 置き場を開けない、D を作れない、名前を使い切った、乱数が取れない (`NotImplementedError`) も、その置き場では作れないとして次へ進む
- ソケットのディレクトリの realpath が引けなければ、何も作らずに中心の経路を使えないとする
- Apple の `O_SEARCH` は `0x40000000 | O_DIRECTORY` を書き下す (`os.O_SEARCH` の無い macOS 向けのビルドがある)

| 順 | D の置き場 | 条件 |
|---|---|---|
| 1 | `${TMPDIR:-/tmp}` | 空なら `/tmp`。相対パスも可 (開いた fd で掴む) |
| 2 | `/tmp` | 1 で作れない時だけ。1 と区切り単位で同じ綴りなら飛ばす |
| 3 | ソケットのディレクトリ | 2 でも作れない時だけ。名前は `.sockat.XXXXXXXXXX` |

- 置き場の安全と長さは問わない
- 置き場の候補は `_route.Config.own_dir_places` で、試験から差し替える

### 3. D の fd を基準にする手段

| 順 | 手段 | OS | 作り方 | 使えない時 (次の手段へ) |
|---|---|---|---|---|
| 1 | スレッド cwd | Linux / Android | 使い捨ての `threading.Thread` で、ctypes の `unshare(CLONE_FS)` → `os.fchdir(<D>)` → `R/<ファイル名>` で標準の `bind` / `connect` | `prctl(PR_GET_SECCOMP)` が 0 でない (`unshare` を呼ばない)、`unshare` を断られた、スレッドを作れない |
| 1 | スレッド cwd | Apple の OS | 使い捨ての `threading.Thread` で、ctypes の `pthread_fchdir_np(<D>)` → 同上 | `pthread_fchdir_np` が無い・失敗した、スレッドを作れない |
| 1 | bindat / connectat | FreeBSD | ctypes の `bindat(<D>, ...)` / `connectat(<D>, ...)` に `R/<ファイル名>` | ファイル名 > 92 |
| 2 | /proc 版 | Linux / Android | `/proc/self/fd/<D>/R/<ファイル名>` を標準の `bind` / `connect` に渡す | `/proc/self/fd/<fd>` を引けない、綴り > 107 (ファイル名およそ 79 まで) |

- 長さと、手段を使えるか (seccomp、`pthread_fchdir_np` の有無、`/proc`) は D を作る前に見る。どの手段も使えなければ何も作らない。/proc 版の長さはソケットのディレクトリの fd の番号で見積もり、D の fd で確かめ直す
- スレッド cwd がスレッドの中で使えなかった時は、同じ D で /proc 版に進む
- ソケットは呼び出し元のスレッドで `socket.socket(AF_UNIX, SOCK_STREAM)` で作り、listen はスレッドが終わってから呼び出し元がする
- スレッドの `join` が例外 (`KeyboardInterrupt` 等) で抜けたら、ソケットはスレッドが終わる時にスレッドが閉じる
- NetBSD / OpenBSD / illumos には手段が無い

### 4. 中心の経路が使えない時の bind

`ENAMETOOLONG` (標準で長すぎるパスと同じ)。SPEC の fork の経路の場所。`_route.bind_without_central` の 1 か所にまとめ、ハブの D12-Q3 の裁定で変える。

| 届かない所 | 理由 |
|---|---|
| NetBSD / OpenBSD / illumos の長いパス全部 | 手段が無い |
| スレッド cwd も /proc 版も使えない Linux / Android | 同上 |
| `pthread_fchdir_np` を使えない Apple の OS | 同上 |
| ファイル名 > 96 (Linux)、> 92 (Apple の OS / FreeBSD)、/proc 版だけ使える Linux はおよそ 79 | `R/<ファイル名>` が sockaddr に入らない |
| D をどこにも作れない | 中心の経路が使えない |

### 5. connect の予備の経路

| OS | 試す順番 |
|---|---|
| Linux / Android (`/proc` を使える) | /proc → fd で掴む形 ([DR-0002](DR-0002-unsafe-base-held-by-fd.md)) → symlink |
| Linux / Android (`/proc` を使えない) | 相対パス → symlink |
| Apple の OS | 相対パス → 広げた sockaddr → symlink |
| FreeBSD | connectat (ソケットのディレクトリの fd とファイル名) |
| NetBSD / OpenBSD / illumos / その他の Unix | 相対パス → symlink |

- `/proc` を使えるかは、決定 1 の 2 で開いた fd (開けなければ `/` を開いた fd) の `/proc/self/fd/<fd>` を `os.stat` で引けるかで決める
- FreeBSD でソケットのディレクトリを開けなければ、connectat は使えず `ENAMETOOLONG`
- symlink の置き場は `TMPDIR` (絶対パスで `/tmp` と違う時)、`/tmp` の順で、安全な置き場 (sticky で持ち主が root か自分、または持ち主が自分で group と other に書き込み権限が無い) だけを使う
- 最後まで届かなければ `ENAMETOOLONG`

### 6. bind が使わない経路

| 経路 | 理由 |
|---|---|
| /proc (D を通さない形) | 綴りの fd の番号が後で使い回され、別のディレクトリの同名ファイルに当たる |
| 相対パス | cwd が変わると別の場所に当たる |
| 広げた sockaddr | listener のアドレスが途中で切れ、標準の `getsockname` が読み過ぎる |
| 置き場の symlink | 中心の経路で足りる |

- 返すアドレスは元のパス ([DR-0003](DR-0003-report-the-given-path.md)) だが、カーネルの綴りも何にも当たらないようにする (`socket.socket.getsockname` を直接呼ぶコード、`socket.fromfd` で作り直したソケットはカーネルの綴りを見る)

### 7. bind_ex

| 項目 | 決定 |
|---|---|
| 形 | `listener, discard = sockat.bind_ex(path)`。`listener` は `sockat.bind(path)` と同じ。`discard` は引数なしの関数 |
| 覚えるもの | 経路の中で bind が通った直後 (listen の前) に、ソケットのファイルの realpath (`strict=True`) と、それを `os.lstat` した `(st_dev, st_ino)`。bind が返った後に誰かがパスに置いたファイルを覚えないため |
| `discard()` | その realpath を `os.lstat` し、`(st_dev, st_ino)` が同じ時だけ `os.unlink`。無い・別のファイルに置き換わっていれば何もしない。何度呼んでもよい。それ以外のエラー (`EACCES` 等) は `OSError` |
| listener | `discard` は閉じない。閉じるのは利用者 (asyncio に渡したら asyncio) |
| 短いパス | 長いパスと同じに動く |
| Linux の abstract のアドレス | `discard` は何もしない |
| 覚えるのに失敗した時 | ソケットを閉じてそのエラーを返す |

- 名前は仮。listener を閉じない形はハブの D12-Q4 の統括推し (α-a) で、SPEC の今の文言 (「`discard()` で listener を閉じ」) と違う。裁定で変える
- 確かめてから消すまでの間に他のプロセスがファイルを置き換えることは防げない (inode を指定して消すシステムコールが無い)

## Alternatives Considered

- 案 A: 中心の経路が使えない bind に、置き場の symlink か相対パスを残す
  - 不採用理由: ハブの DR-0012 が bind から外した (綴りが後で無関係なファイルに当たりうる)。戻すかはハブの D12-Q3 で決める
- 案 B: 呼び出し元のスレッドで `pthread_fchdir_np` し、`-1` で戻す
  - 不採用理由: 呼び出し元がスレッドに付けた cwd を消しうる (SPEC)。使い捨てのスレッドなら触らずに済む
- 案 C: Linux で D を `O_RDONLY` で開いて `fchmod` する
  - 不採用理由: umask が持ち主の読み取りを削ると非 root では開けず、D が置き場に残る。`O_PATH` の fd には `fchmod` ができない (`EBADF`) ので、`/proc/self/fd/<fd>` か開き直しで fd が指す実体に効かせる
- 案 D: `discard()` が listener も閉じる
  - 不採用理由: listener を asyncio 等に渡した後は持ち主がそちらに移る。形はハブの D12-Q4 で決める

## Consequences

- bind のファイル名の上限は `R/` の 11 バイト分短い (Linux 96、Apple の OS / BSD 92。/proc 版はおよそ 79)
- NetBSD / OpenBSD / illumos では長いパスの bind が届かない (ハブの D12-Q3 で kawaz 裁定待ち)
- 長いパスの bind / connect のたびに D と R を作って消す
- 途中でプロセスが死ぬと、置き場に `sockat.XXXXXXXXXX`、ソケットのディレクトリに `.sockat.XXXXXXXXXX` が残る
- Apple の OS で umask が持ち主の検索権限を削ると (非 root で 0777 / 0177 等)、D を開けず置き場に残る。`O_RDONLY` で開く OS では読み取り権限で同じ (ハブの D12-Q5)
- R はソケットのディレクトリの realpath を指すので、R を作った後のディレクトリの rename や symlink の張り替えは反映されない
- 試験は `tests/test_central.py` (中心の経路)、`tests/test_ex.py` (`bind_ex`)

## 関連

- [ハブの DR-0012](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0012-own-directory-and-thread-cwd.md)
- [ハブの SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)
- [ハブの QUESTIONS](https://github.com/kawaz/sockat/blob/main/docs/QUESTIONS.md) (D12-Q3、D12-Q4、D12-Q5)
- [DR-0001](DR-0001-python-port-design.md)、[DR-0002](DR-0002-unsafe-base-held-by-fd.md)、[DR-0003](DR-0003-report-the-given-path.md)
