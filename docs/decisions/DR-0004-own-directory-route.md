# DR-0004: D を通す経路 (自分専用のディレクトリ D と R) の Python での作り方、bind の discard

- Status: Accepted (ハブの DR-0012 の Python での作り方。統括の判断 2026-11-19。D をどこにも作れない時の bind の D 無しの形はハブの D12-Q1 の kawaz 裁定 (a)。D を通す経路が使えない時の `bind` が `ENAMETOOLONG` なのはハブの D12-Q3 の kawaz 裁定 (a)。`discard` が listener を閉じない形と名前はハブの D12-Q4 の kawaz 裁定 (ハブの DR-0015)。開けない D を消すのはハブの D12-Q5 の kawaz 裁定)
- Date: 2026-11-19

## Context

- ハブの [DR-0012](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0012-own-directory-and-thread-cwd.md) が、長いパスは自分専用のディレクトリ D を fd で掴み、その中の乱数の symlink R を通して `R/<ファイル名>` で bind / connect する形にした。bind は proc-direct・relative・widened・symlink を使わない。connect はそれらと D を通す経路を、書き込み (D・R・symlink を作る) もスレッドも要らない経路を先にした OS ごとの順 (ハブの SPEC の「connect の順」) で試す
- SPEC の bind は D を通す経路が使えない時に fork-cwd の経路へ進むが、Python 版は fork の経路を作らない ([DR-0001](DR-0001-python-port-design.md))
- SPEC の bind は D をどこにも作れない時、D 無しの形 (ソケットのディレクトリに R を作り、ソケットのディレクトリの fd を基準にする) を D を通す経路と同じ手段の順に試し (thread-cwd-sockdir → proc-sockdir)、最後に fork-cwd-sockdir へ進む
- SPEC は後始末つきの bind (`_ex`) を足した
- ハブの [DR-0016](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0016-bind-through-file-symlink.md) が、bind が symlink を辿る OS では standard の次にソケットのファイルを指す symlink を通して bind する経路 (symlink-file) を足した

## Decision

### 1. 試す順番

| 順 | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| 1 | `sun_path` − 1 以下なら標準にそのまま (環境も読まない) | 同じ |
| 2 | ソケットのディレクトリを `os.stat` で引く (無ければ `ENOENT`、ディレクトリでなければ `ENOTDIR`)。fd が要る経路のために開く | 同じ |
| 3 | symlink-file (bind が symlink を辿らないと確かめた OS 以外。決定 8) | connect の順 (決定 5)。D を通す経路もこの順の中で試す |
| 3 の次 | D を通す経路 (決定 3 の順) | (同上) |
| 4 | D をどこにも作れなければ D 無しの形。それも使えない時と、D を作れて手段が全部使えなかった時は `ENAMETOOLONG` (決定 4) | 順の最後まで使えなければ `ENAMETOOLONG` |

- ソケットのディレクトリの fd が要るのは、ソケットのディレクトリに作る D、bind の D 無しの形と、connect の proc-direct・`/proc` を使えるかの確かめ・at-direct。開けなければ (`O_RDONLY` で開く OS で読み取り権限が無い等) それらを使えないとして他を試す
- ディレクトリ (ソケットのディレクトリ、D の置き場) の開き方: Linux / Android は `O_PATH | O_DIRECTORY`、Apple の OS は `O_SEARCH` (`EISDIR` / `EINVAL` なら `O_RDONLY`)、他は `O_RDONLY | O_DIRECTORY` (読み取り権限が要る)
- 入口は `_route.run` (と、通った経路・飛ばした経路も返す `_route.reach`)。長いパスの bind は `routes` が `bind_symlink_file` (決定 8) と `through_d` を試し、使えなければ `bind_past_d` に進む。connect は `connect_routes` が connect の順 (`_route.connect_order`) の経路を 1 つずつ試す

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

- 確かめに落ちた D は他人の物かもしれないので、中身も含めて触らない。確かめが通った後に 0700 にそろえられない・R を作れない D は消して、次の置き場へ進む
- 作った直後の D を開けない時 (umask が持ち主の検索・読み取り権限を削った時) は、`os.stat(<D の名前>, dir_fd=<置き場>, follow_symlinks=False)` が確かめと同じ条件 (ディレクトリ・持ち主が実効 uid・group と other に権限が無い) なら `os.rmdir(<D の名前>, dir_fd=<置き場>)` で消し、次の置き場へ進む。条件に合わない物と空でない物は触らない (`rmdir` は空のディレクトリしか消さない) (ハブの D12-Q5)
- 置き場を開けない、D を作れない、名前を使い切った、乱数が取れない (`NotImplementedError`) も、その置き場では作れないとして次へ進む
- ソケットのディレクトリの realpath は決定 1 の 2 で `os.stat` と一緒に引き、引けなければそのエラーを返す (長さの判定や D を作るより前)
- Apple の `O_SEARCH` は `0x40000000 | O_DIRECTORY` を書き下す (`os.O_SEARCH` の無い macOS 向けのビルドがある)

| 順 | D の置き場 | 条件 |
|---|---|---|
| 1 | `${TMPDIR:-/tmp}` | 空なら `/tmp`。相対パスも可 (開いた fd で掴む) |
| 2 | `/tmp` | 1 で作れない時だけ。1 と区切り単位で同じ綴りなら飛ばす |
| 3 | ソケットのディレクトリ | 2 でも作れない時だけ。名前は `.sockat.XXXXXXXXXX` |

- 置き場の安全と長さは問わない
- 置き場の候補は `_route.Config.own_dir_places` で、試験から差し替える

### 3. D の fd を基準にする手段 (経路)

| 順 | 経路 | OS | 作り方 | 使えない時 (次の手段へ) |
|---|---|---|---|---|
| 1 | thread-cwd | Linux / Android | 使い捨ての `threading.Thread` で、ctypes の `unshare(CLONE_FS)` → `os.fchdir(<D>)` → `R/<ファイル名>` で標準の `bind` / `connect` | `prctl(PR_GET_SECCOMP)` が 0 でない (`unshare` を呼ばない)、`unshare` を断られた、スレッドを作れない |
| 1 | thread-cwd | Apple の OS | 使い捨ての `threading.Thread` で、ctypes の `pthread_fchdir_np(<D>)` → 同上 | `pthread_fchdir_np` が無い・失敗した、スレッドを作れない |
| 1 | at | FreeBSD | ctypes の `bindat(<D>, ...)` / `connectat(<D>, ...)` に `R/<ファイル名>` | ファイル名 > 92 |
| 2 | proc | Linux / Android | `/proc/self/fd/<D>/R/<ファイル名>` を標準の `bind` / `connect` に渡す | `/proc/self/fd/<fd>` を引けない、綴り > 107 (ファイル名およそ 79 まで) |

- 長さと、手段を使えるか (seccomp、`pthread_fchdir_np` の有無、`/proc`) は D を作る前に見る。どの手段も使えなければ何も作らない。proc の長さはソケットのディレクトリの fd の番号で見積もり、D の fd で確かめ直す。収まらなければ proc を使えないとして次へ進む。D は後の経路と共有し、片付けは呼び出しの最後に 1 回
- thread-cwd がスレッドの中で使えなかった時は、同じ D で proc に進む (bind も connect も)
- ソケットは呼び出し元のスレッドで `socket.socket(AF_UNIX, SOCK_STREAM)` で作り、listen はスレッドが終わってから呼び出し元がする
- スレッドの `join` が例外 (`KeyboardInterrupt` 等) で抜けたら、ソケットはスレッドが終わる時にスレッドが閉じる
- NetBSD / OpenBSD / illumos には手段が無い

### 4. D を通す経路が使えない時の bind

`_route.bind_past_d` の 1 か所にまとめる。

- D をどこにも作れない時 (`through_d` が `Skip.NO_OWN_DIR` を返す) は、D 無しの形 (`_route.bind_sockdir`) を試す
- D 無しの形も使えない時と、D を作れて手段が全部使えなかった時は `ENAMETOOLONG` (標準で長すぎるパスと同じ)。SPEC の fork-cwd の経路の場所 (ハブの D12-Q3 の kawaz 裁定 (a))。最終手段を使う `sockat.ex.bind` はここで symlink-dir に進む ([DR-0006](DR-0006-sockat-ex.md))
- 手段が無くて D を試さなかった時は、D 無しの形も試さない (綴りが D の形より長く、手段の確かめも同じなので、必ず使えない)

D 無しの形:

| 手順 | Python での作り方 |
|---|---|
| 確かめる | ソケットのディレクトリを開けていること (決定 1 の 2 の fd)、手段を使えること (決定 3 の確かめを綴り `.sockat.XXXXXXXXXX/<ファイル名>` で。proc-sockdir の長さはソケットのディレクトリの fd の番号で正確に分かる)。欠ければ何も作らない |
| R を作る | `os.symlink(<ソケットのディレクトリの realpath>, <R>, dir_fd=<ソケットのディレクトリ>)`。R は `.sockat.` + `secrets` の英数 62 種 10 文字。`FileExistsError` で引き直し、16 回で打ち切る |
| bind | 決定 3 の手段を同じ順に、D の fd の代わりにソケットのディレクトリの fd を基準にして `R/<ファイル名>`: thread-cwd-sockdir (FreeBSD は at-sockdir) → proc-sockdir (`/proc/self/fd/<ソケットのディレクトリの fd>/R/<ファイル名>`) |
| 片付け | 自分が作れた R だけを、成功でも失敗でも `os.unlink(<R>, dir_fd=<ソケットのディレクトリ>)`。消す失敗は無視する |

- 綴りは 19 バイト + ファイル名。ファイル名は Linux 88、Apple の OS / FreeBSD 84 まで (proc-sockdir はおよそ 72)
- 経路は `Route.THREAD_CWD_SOCKDIR` / `Route.AT_SOCKDIR` / `Route.PROC_SOCKDIR`
- bind が実際に失敗したら (`EADDRINUSE` 等) そのエラーを返し、後の手段を試さない

| 届かない所 | 理由 |
|---|---|
| NetBSD / OpenBSD / illumos の長いパス全部 | 手段が無い |
| thread-cwd も proc も使えない Linux / Android | 同上 |
| `pthread_fchdir_np` を使えない Apple の OS | 同上 |
| ファイル名 > 96 (Linux)、> 92 (Apple の OS / FreeBSD)、proc だけ使える Linux はおよそ 79 | `R/<ファイル名>` が sockaddr に入らない |
| D をどこにも作れず、ファイル名 > 88 (Linux)、> 84 (Apple の OS / FreeBSD)、proc-sockdir だけ使える Linux はおよそ 72 | `.sockat.XXXXXXXXXX/<ファイル名>` が sockaddr に入らない |
| D をどこにも作れず、ソケットのディレクトリを開けない、または R を作れない | D 無しの形が使えない |

### 5. connect の順

`_route.connect_routes` が、OS ごとの順 (`_route.connect_order`) の経路を 1 つずつ試す。書き込み (D・R・symlink を作る) もスレッドも要らない経路を先に、D を作る経路とスレッドを立てる経路をその後にする (ハブの SPEC の「connect の順」)。

| OS | 試す順番 |
|---|---|
| Linux / Android (`/proc` を使える) | proc-direct → thread-cwd → proc → proc-held ([DR-0002](DR-0002-unsafe-base-held-by-fd.md)) → symlink-file |
| Linux / Android (`/proc` を使えない) | relative → thread-cwd → symlink-file |
| Apple の OS | relative → widened → thread-cwd → symlink-file |
| FreeBSD | at-direct (ソケットのディレクトリの fd とファイル名) → at |
| NetBSD / OpenBSD / illumos / その他の Unix | relative → symlink-file |

- `/proc` を使えるかは、決定 1 の 2 で開いた fd (開けなければ `/` を開いた fd) の `/proc/self/fd/<fd>` を `os.stat` で引けるかで決める
- 各経路は関数 1 つ (`_route.CONNECT_ROUTES`) で、connect したソケットか、使えない理由を返す。使えない経路は理由と一緒に `skipped` に、実際に connect した経路は失敗しても `route` に記録する (`_route.Trail`)。試験は順を `Config.connect_order` で差し替えて経路を強制する
- D は 1 回の connect で 1 度だけ、それを使う最初の経路 (thread-cwd・at・proc・proc-held) が決定 2 の置き場の順に作り、後の経路で共有する (`_route.ConnectCall`)。thread-cwd・at・proc が作る時は R も作る (R を作れない置き場は作れないとして次へ)。proc-held は D の中に `s` を足す。どこにも作れなかったら、D を使う経路はどれも「D の置き場が無い」理由で飛ばし、作り直さない。D は connect の最後に、成功でも失敗でも片付ける
- 長さと手段を使えるかは、D を使う経路ごとに D を作る前に見る (決定 3 と同じ)。使えない経路は D を作らずに飛ばす
- FreeBSD でソケットのディレクトリを開けなければ at-direct は使えず、at に進む
- symlink-file の置き場は `TMPDIR` (絶対パスで `/tmp` と違う時)、`/tmp` の順で、安全な置き場 (sticky で持ち主が root か自分、または持ち主が自分で group と other に書き込み権限が無い) だけを使う。置き場の候補を複数試しても `skipped` には 1 件
- 最後まで届かなければ `ENAMETOOLONG`

### 6. bind が使わない経路

| 経路 | 理由 |
|---|---|
| proc-direct (R を通さない `/proc/self/fd/<fd>/<ファイル名>`) | 綴りの fd の番号が後で使い回され、別のディレクトリの同名ファイルに当たる |
| relative | cwd が変わると別の場所に当たる |
| widened | listener のアドレスが途中で切れ、標準の `getsockname` が読み過ぎる |
| symlink-dir | D を通す経路で足りる。最終手段を使う `sockat.ex.bind` だけが最後に使う ([DR-0006](DR-0006-sockat-ex.md)) |

- 返すアドレスは元のパス ([DR-0003](DR-0003-report-the-given-path.md)) だが、カーネルの綴りも何にも当たらないようにする (`socket.socket.getsockname` を直接呼ぶコード、`socket.fromfd` で作り直したソケットはカーネルの綴りを見る)

### 7. bind の discard

| 項目 | 決定 |
|---|---|
| 形 | `listener, discard, *_ = sockat.ex.bind(path)` ([DR-0006](DR-0006-sockat-ex.md))。`listener` は `sockat.bind(path)` と同じ。`discard` は引数なしの関数 |
| 覚えるもの | 経路の中で bind が通った直後 (listen の前) に、ソケットのファイルの realpath (`strict=True`) と、それを `os.lstat` した `(st_dev, st_ino)`。bind が返った後に誰かがパスに置いたファイルを覚えないため |
| `discard()` | その realpath を `os.lstat` し、`(st_dev, st_ino)` が同じ時だけ `os.unlink`。無い・別のファイルに置き換わっていれば何もしない。何度呼んでもよい。それ以外のエラー (`EACCES` 等) は `OSError` |
| listener | `discard` は閉じない。閉じるのは利用者 (asyncio に渡したら asyncio) |
| 短いパス | 長いパスと同じに動く |
| Linux の abstract のアドレス | `discard` は何もしない |
| 覚えるのに失敗した時 | ソケットを閉じてそのエラーを返す |

- `discard` の名前と、listener を閉じない形はハブの D12-Q4 の kawaz 裁定で確定 (ハブの [DR-0014](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0014-discard-removes-only.md))
- 確かめてから消すまでの間に他のプロセスがファイルを置き換えることは防げない (inode を指定して消すシステムコールが無い)

### 8. bind の symlink-file

ハブの [DR-0016](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0016-bind-through-file-symlink.md) の Python での作り方。

| 項目 | 決定 |
|---|---|
| 試す OS | `_route.Config.bind_symlink_file` (既定は `_route.BIND_DOES_NOT_FOLLOW_A_SYMLINK` でない OS で真)。bind が最後の要素の symlink を辿らないと実測した Linux / Android、FreeBSD、OpenBSD、illumos では試さない。辿るのは macOS と NetBSD (実測 2026-11-19、ハブの SPEC)。他の OS は実行時に見分ける。試験はこれを Linux でも真にして、辿らないカーネルを実行時に見分けることを確かめる |
| 置き場と symlink | connect の symlink-file と同じ (`Config.link_places`、安全な置き場、`<置き場>/sockat.XXXXXXXXXX` が `sun_path` に収まる)。symlink の先は元のパス (相対パスなら cwd を前に付ける)。bind は広げた sockaddr を使わない |
| bind | 綴りで標準の `bind`、`listen` (`_route.symlink_file_route`)。成功でも失敗でも symlink を消す |
| 失敗した時 | 元のパスを `os.lstat` し、物があればその例外を返して次を試さない。無いか `lstat` が失敗したら、カーネルが辿らなかったとみなし、次の置き場も試さずにこの経路を使えないとして `skipped` に入れ、D を通す経路に進む |
| 失うもの | `respelled` |

- `sockat.ex.bind` は bind が通った直後に元のパスの realpath を覚えるので、symlink-file を通っても `discard()` は元のパスのファイルを消す
- `_route.Route.SYMLINK_FILE` は connect の symlink-file と同じ名前 (SPEC の「経路の名前」で bind と connect の両方に出る)。bind の最終手段は `_route.Route.SYMLINK_DIR`

## Alternatives Considered

- 案 A: D を通す経路が使えない `bind` に、置き場の symlink-dir か相対パスを残す
  - 不採用理由: ハブの DR-0012 が bind から外した (綴りが後で無関係なファイルに当たりうる)。既定の bind には戻さない (ハブの D12-Q3 の kawaz 裁定 (a))
- 案 B: 呼び出し元のスレッドで `pthread_fchdir_np` し、`-1` で戻す
  - 不採用理由: 呼び出し元がスレッドに付けた cwd を消しうる (SPEC)。使い捨てのスレッドなら触らずに済む
- 案 C: Linux で D を `O_RDONLY` で開いて `fchmod` する
  - 不採用理由: umask が持ち主の読み取りを削ると非 root では開けず、D が置き場に残る。`O_PATH` の fd には `fchmod` ができない (`EBADF`) ので、`/proc/self/fd/<fd>` か開き直しで fd が指す実体に効かせる
- 案 D: `discard()` が listener も閉じる
  - 不採用理由: listener を asyncio 等に渡した後は持ち主がそちらに移る (ハブの DR-0014)
- 案 E: D をどこにも作れない時の bind も `ENAMETOOLONG` にする
  - 不採用理由: ハブの D12-Q1 で kawaz が D 無しの形 (a) を裁定した。fork の経路の無い Python でも、ソケットのディレクトリに R を作れれば届く
- 案 F: 作った直後に開けない D は、他人の物かもしれないので残す
  - 不採用理由: umask が持ち主の権限を削る環境で呼ぶたびに積もる。名前で確かめて `rmdir` すれば、すり替えられても最悪は他人の空のディレクトリが 1 つ消えるだけ (ハブの D12-Q5)

## Consequences

- bind のファイル名の上限は `R/` の 11 バイト分短い (Linux 96、Apple の OS / BSD 92。proc はおよそ 79)。D をどこにも作れない時は D 無しの形の 19 バイト分 (Linux 88、Apple の OS / BSD 84。proc-sockdir はおよそ 72)
- OpenBSD / illumos では長いパスの bind が届かない (ハブの D12-Q3 の kawaz 裁定 (a))。NetBSD は symlink-file で届く
- 長いパスの bind / connect のたびに D と R を作って消す
- 途中でプロセスが死ぬと、置き場に `sockat.XXXXXXXXXX`、ソケットのディレクトリに `.sockat.XXXXXXXXXX` (D か、D 無しの形の R) が残る
- R はソケットのディレクトリの realpath を指すので、R を作った後のディレクトリの rename や symlink の張り替えは反映されない
- 試験は `tests/test_through_d.py` (D を通す経路と D 無しの形)、`tests/test_ex.py` (`discard`)

## 関連

- [ハブの DR-0012](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0012-own-directory-and-thread-cwd.md)
- [ハブの SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)
- [ハブの QUESTIONS](https://github.com/kawaz/sockat/blob/main/docs/QUESTIONS.md) (D12-Q1、D12-Q3、D12-Q4、D12-Q5)
- [DR-0005](DR-0005-bind-any.md)
- [DR-0001](DR-0001-python-port-design.md)、[DR-0002](DR-0002-unsafe-base-held-by-fd.md)、[DR-0003](DR-0003-report-the-given-path.md)
