# DR-0006: sockat.ex (後始末・診断つきの呼び出しと最終手段) の Python での形

- Status: Accepted (ハブの DR-0015 の Python での形。モジュール `sockat.ex`、関数名 `bind` / `connect` / `connect_nonblocking`、引数 `last_resort`、推奨コードをモジュールを通して書くことは kawaz 裁定 2026-11-19。戻り値の型の形は統括の判断 2026-11-19)
- Date: 2026-11-19

## Context

- ハブの [DR-0015](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0015-ex-with-last-resort-and-diagnostics.md) が、any 系をやめて、既定の操作 (`bind`、`connect`、`connect_nonblocking`) ごとに後始末・診断つきの `_ex` を置く形にした
  - `_ex` は既定の操作と同じ経路を試し、通った経路の名前 (`route`)、試して使えなかった経路 (`skipped`、各要素は `{name, reason}`)、失ったもの (`losses`) を返す。bind の方は加えて `discard` を返す
  - 引数で選べるのは「最終手段を使うか」の 1 つだけ (既定は使わない)。bind の最終手段は symlink-dir、connect の最終手段は fork-cwd-direct で、fork の無い言語は fork-cwd-direct を理由付きで `skipped` に入れて次へ進む
  - 失敗した時もエラーに `skipped` (と、実際に bind / connect して失敗した経路の名前) を付ける
- Python には fork の経路が無い ([DR-0001](DR-0001-python-port-design.md))。最終手段を使う connect は既定の connect と同じ所に届く
- 標準の `socket.socket.connect_ex` は errno を返すメソッドで、`connect_ex` という名前は紛れる

## Decision

### 1. 置き場と名前

| 項目 | 決定 |
|---|---|
| モジュール | サブモジュール `sockat.ex` (`src/sockat/ex.py`) |
| 既定の import | `sockat/__init__.py` は `sockat.ex` を import しない。`import sockat` だけでは `sockat.ex` は見えない。使う人が `import sockat.ex` で選ぶ |
| 関数 | `sockat.ex.bind`、`sockat.ex.connect`、`sockat.ex.connect_nonblocking` (SPEC の `bind_ex`、`connect_ex`、`connect_nonblocking_ex`) |
| 引数 | `path` と、キーワード専用の `last_resort: bool = False` |
| 公開する名前 | `bind`、`connect`、`connect_nonblocking`、`Bound`、`Connected`、`Skipped`、`Loss` |
| 推奨コード | README・MANUAL の例は `sockat.ex.connect(...)` とモジュールを通して書く。`from sockat.ex import connect` と取り込む書き方は勧めない (既定の `sockat.connect` と紛れる) |

- トップレベルの `sockat.bind_ex` と、`sockat.any` / `bind_any` は置かない

### 2. 戻り値

```python
class Skipped(NamedTuple):
    name: str
    reason: str


class Bound(NamedTuple):
    listener: socket.socket
    discard: Callable[[], None]
    losses: tuple[Loss, ...]
    route: str
    skipped: tuple[Skipped, ...]


class Connected(NamedTuple):
    stream: socket.socket
    losses: tuple[Loss, ...]
    route: str
    skipped: tuple[Skipped, ...]
```

| 項目 | 決定 |
|---|---|
| 型 | `NamedTuple`。`listener, discard, losses, route, skipped = sockat.ex.bind(path)`、`stream, losses, route, skipped = sockat.ex.connect(path)` と分解できる。`listener, discard, *_ = sockat.ex.bind(path)` とも書ける |
| `listener` / `stream` | 既定の `sockat.bind` / `sockat.connect` / `sockat.connect_nonblocking` が返すものと同じ |
| `discard` | [DR-0004](DR-0004-own-directory-route.md) の決定 7 |
| `losses` | `Loss` の並び。`Loss` は `enum.StrEnum` で、`str()` と `.value` が SPEC の名前。SPEC の名前を全部持つ (`FORKED` は sockat-py では付かないが、SPEC の語彙として置く) |
| `route` | `_route.Route` の値 (SPEC の経路の名前の文字列)。`Route` の enum は公開しない |
| `skipped` | 試した順。経路の名前と理由 (短い英語の句、診断用) |

`losses` に付くもの:

| 経路 | `losses` |
|---|---|
| standard (絶対パス、abstract のアドレス) | 空 |
| standard (相対パスの bind) | `cwd-relative` |
| bind の symlink-file、thread-cwd、at、proc、thread-cwd-sockdir、at-sockdir、proc-sockdir | `respelled` |
| symlink-dir (持ち主だけが書ける置き場) | `respelled` |
| symlink-dir (共有の置き場) | `respelled`、`recreatable-spelling` |
| connect の経路 | 空 (connect の綴りは相手に残らない) |

bind の `skipped` に入るもの (長いパス):

| 位置 | 経路 | 理由の例 |
|---|---|---|
| 最初 | standard | `path too long` |
| 次 (Linux / Android 以外) | symlink-file | `no place for the symlink`、`symlink not followed by bind` |
| D を通す経路 | この OS にある経路 (Linux は thread-cwd、proc。Apple の OS は thread-cwd。FreeBSD は at) | `spelling too long`、`seccomp filter`、`no pthread_fchdir_np`、`thread cwd turned off`、`/proc unusable`、`thread cwd refused`、`no place for D` |
| fork-cwd | fork-cwd | `no fork route in sockat-py` |
| D 無しの形 (D をどこにも作れない時だけ) | thread-cwd-sockdir (FreeBSD は at-sockdir)、proc-sockdir | 上と同じ、`socket directory not opened`、`R not created` |
| fork-cwd | fork-cwd-sockdir (D 無しの形も使えない時) | `no fork route in sockat-py` |

- connect の `skipped` は、connect の順 ([DR-0004](DR-0004-own-directory-route.md) の決定 5) の経路を試した順に、使えなかった理由と一緒に積む
- fork の経路 (fork-cwd、fork-cwd-sockdir、最終手段を使う connect の fork-cwd-direct) は、SPEC の順でその位置に来た時に「sockat-py に無い」理由で入れる。SPEC の他の言語では fork で届く所に来たことが、利用者の手元で分かる

### 3. 最終手段

| 関数 | `last_resort=True` の時 |
|---|---|
| `sockat.ex.bind` | 既定の bind が `ENAMETOOLONG` になる所 (`_route.bind_past_d` の最後) で symlink-dir を試す (`_route.symlink_dir_route`) |
| `sockat.ex.connect`、`sockat.ex.connect_nonblocking` | connect の順の最後まで使えなかった時、fork-cwd-direct を `no fork route in sockat-py` の理由で `skipped` に入れてから `ENAMETOOLONG` |

symlink-dir:

| 手順 | Python での作り方 |
|---|---|
| 置き場の候補 | `XDG_RUNTIME_DIR`、`TMPDIR` (空でない絶対パスの時)、`/tmp` の順。`/tmp` か前の候補と区切り単位で同じ綴りは飛ばす (`_route.Config.bind_link_places`) |
| 置き場の種類 | `os.stat` (symlink を辿る) で、ディレクトリ・持ち主が実効 uid・`mode & 0o022 == 0` なら持ち主だけが書ける置き場、ディレクトリ・sticky・持ち主が root か実効 uid なら共有の置き場、どちらでもなければ使わない (`_route.place_kind`) |
| 試す順 | 持ち主だけが書ける置き場を候補の順に、その後に共有の置き場を候補の順に |
| 長さ | 何かを作る前に、綴り `<置き場>/sockat.XXXXXXXXXX/<ファイル名>` が `sun_path` − 1 に収まるかを確かめる。収まらない候補は飛ばす (bind は広げた sockaddr を使わない) |
| symlink | `os.symlink(<ソケットのディレクトリの realpath>, <置き場>/sockat.XXXXXXXXXX)`。名前は `secrets` の英数 62 種 10 文字、`FileExistsError` で引き直し 16 回まで。作れない候補は飛ばす |
| bind | 綴りで標準の `bind`、`listen`。成功でも失敗でも symlink を消す。bind の失敗はそのまま返し、次の置き場を試さない |
| 届かない時 | どの候補でも届かなければ `ENAMETOOLONG` |

- 経路の名前は `_route.Route.SYMLINK_DIR`

### 4. 失敗した時

| 項目 | 決定 |
|---|---|
| 例外 | 既定の呼び出しと同じ例外 (`OSError` とそのサブクラス、`ValueError`) を、型も `errno` も変えずに送出する |
| 付けるもの | 例外の属性 `skipped` (`tuple[Skipped, ...]`、失敗するまでに使えなかった経路) と `route` (実際に bind / connect して失敗した経路の名前、そこまで進まなかった時は `None`) |
| `route` が `None` | ディレクトリが無い (`ENOENT`) 等、経路より前に決まるエラー、どの経路も使えない `ENAMETOOLONG`、引数の誤り |

理由: 属性を足すだけなら、`except FileNotFoundError` や `e.errno` で分けている呼び出し側がそのまま動く。

## Alternatives Considered

- 案 A: トップレベルに `sockat.bind_ex` / `sockat.connect_ex` / `sockat.connect_nonblocking_ex` を置く
  - 不採用理由: `connect_ex` は標準の `socket.socket.connect_ex` (errno を返す) と紛れる。補完で既定の呼び出しと並び、選ばずに使えてしまう (kawaz 裁定)
- 案 B: `from sockat.ex import connect` と取り込む例を書く
  - 不採用理由: 取り込んだ先で既定の `sockat.connect` と見分けにくい (kawaz 裁定)
- 案 C: 戻り値を frozen な dataclass にする (位置で分解できない)
  - 不採用理由: 項目は SPEC で決まり、足すのは `losses` の名前 (値) の側。タプルで分解できる方が Python の慣習に合う
- 案 D: 失敗を `OSError` のサブクラスや新しい例外で包む
  - 不採用理由: errno から決まるサブクラス (`FileNotFoundError` 等) にならず、既定の呼び出しと同じ `except` で拾えない
- 案 E: fork の無い Python では `connect` 側の `_ex` に `last_resort` を持たせない
  - 不採用理由: 3 つの `_ex` の引数がそろわない。SPEC は fork の無い言語も `connect_ex` 系を出し、fork-cwd-direct を `skipped` に入れて次へ進むと決めている (ハブの DR-0015)

## Consequences

- `sockat.any` / `bind_any` / `AnyBinding` と、トップレベルの `sockat.bind_ex` は無くなった ([DR-0005](DR-0005-bind-any.md) を置き換える)。`sockat.bind_ex(path)` の `(listener, discard)` は `listener, discard, *_ = sockat.ex.bind(path)` になる
- NetBSD / OpenBSD / illumos と、D を通す経路の手段を使えない Linux / Apple の OS でも、置き場に収まる長さのファイル名なら最終手段を使う `sockat.ex.bind` で届く
- 試験は `tests/test_ex.py` (`discard`)、`tests/test_ex_bind.py` (bind の診断と最終手段)、`tests/test_ex_connect.py` (connect の診断と最終手段)。symlink-dir を通す試験は、環境から読む設定 (`_route.Config.from_env`) を、既定の bind の経路を全部使えなくする設定 (symlink-file の置き場なし・D の置き場なし・スレッド cwd を切る・`/proc` を無いことにする) に差し替えて行う。FreeBSD では at を使えなくできないので行わない

## 関連

- [ハブの DR-0015](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0015-ex-with-last-resort-and-diagnostics.md)
- [ハブの SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md) (「公開する操作」「symlink-dir」「fork-cwd-direct」「symlink の置き場と名前」)
- [DR-0001](DR-0001-python-port-design.md)、[DR-0004](DR-0004-own-directory-route.md)、[DR-0005](DR-0005-bind-any.md)
