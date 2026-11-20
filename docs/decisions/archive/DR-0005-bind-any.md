# DR-0005: bind_any の Python での形

- Status: Superseded by [DR-0001](../DR-0001-python-port-design.md) (ハブの DR-0017 の作り直し)
- Date: 2026-11-19

## Context

- ハブの [DR-0013](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0013-any-means-reach-at-any-cost.md) が、any 系を「何かを失ってでも届かせる」opt-in の API にした。`bind_any` は既定の bind の経路を全部試した後、最後に置き場の symlink (symlink-dir) を試し、標準の listener と一緒に、失ったものの名前の並び (`losses`)、通った経路の名前 (`route`)、試して使えなかった経路の並び (`skipped`、各要素は `{name, reason}`) を返す。失敗した時もエラーに `skipped` (と、実際に bind して失敗した経路の名前) を付ける
- any 系は既定の API に出さず、使う人が選んで有効にする仕組みの後ろに置く。Python の置き場は各言語の DR で決める
- Python 版の既定の `bind` は、D を通す経路と D 無しの形が使えない所で `ENAMETOOLONG` になる (fork-cwd の経路が無い。[DR-0001](../DR-0001-python-port-design.md)、[DR-0004](DR-0004-own-directory-route.md) の決定 4)。NetBSD / OpenBSD / illumos の長いパス全部と、スレッド cwd も `/proc` も使えない Linux がそこに入る

## Decision

### 1. 置き場

| 項目 | 決定 |
|---|---|
| モジュール | サブモジュール `sockat.any` (`src/sockat/any.py`) |
| 使い方 | `from sockat.any import bind_any` |
| 既定の import | `sockat/__init__.py` は `sockat.any` を import しない。`import sockat` だけでは `sockat.any` も `bind_any` も見えない |
| 公開する名前 | `bind_any`、`AnyBinding`、`Skipped`、`Loss` |

理由:

- import する一手が「失うものを受け入れる」意思表示になる (Rust の feature `any` に当たる)
- 同じ配布物に入るので、入れ方で公開 API が変わらない ([DR-0001](../DR-0001-python-port-design.md) の案 D を避ける)
- 名前 `any` はハブの SPEC と Rust の feature の名前にそろう

### 2. 経路

| 順 | 経路 |
|---|---|
| 1〜 | 既定の `bind` と同じ (`_route.routes`。経路も失敗の扱いも変えない) |
| 最後 | symlink-dir: 既定の `bind` が `ENAMETOOLONG` になる所 (`_route.bind_past_d` の最後) でだけ試す (`_route.symlink_dir_route`) |

symlink-dir:

| 手順 | Python での作り方 |
|---|---|
| 置き場の候補 | `XDG_RUNTIME_DIR`、`TMPDIR` (それぞれ空でない絶対パスの時)、`/tmp` の順。`/tmp` か前の候補と区切り単位で同じ綴りは飛ばす (`_route.Config.bind_link_places`、試験から差し替える) |
| 置き場の種類 | `os.stat` (symlink を辿る) で、ディレクトリ・持ち主が実効 uid・`mode & 0o022 == 0` なら持ち主だけが書ける置き場、ディレクトリ・sticky・持ち主が root か実効 uid なら共有の置き場、どちらでもなければ使わない (`_route.place_kind`) |
| 試す順 | 持ち主だけが書ける置き場を候補の順に全部、次に共有の置き場を候補の順に |
| 長さ | 綴り `<置き場>/sockat.XXXXXXXXXX/<ファイル名>` が `sun_path` − 1 以下か、作る前に確かめる。広げた sockaddr は使わない |
| symlink | `os.symlink(<ソケットのディレクトリの realpath>, <置き場>/sockat.XXXXXXXXXX)`。乱数は `secrets` の英数 62 種 10 文字、`FileExistsError` で引き直し 16 回で打ち切り (connect の symlink と同じ `_route.TempLink`) |
| bind | 綴りで標準の `bind`、`listen`。成功でも失敗でも symlink を消す。bind の失敗はそのまま返し、次の置き場を試さない |
| 届かない | `ENAMETOOLONG` |

- 長いパスの listener は、既定の `bind` と同じく元のパスを返す `PathSocket` ([DR-0003](../DR-0003-report-the-given-path.md))
- 経路の名前は `_route.Route.SYMLINK_DIR` (SPEC の symlink-dir。ソケットのファイルを指す symlink-file とは別の経路)

### 3. 戻り値

```python
class AnyBinding(NamedTuple):
    listener: socket.socket
    losses: tuple[Loss, ...]
    route: str
    skipped: tuple[Skipped, ...]


class Skipped(NamedTuple):
    name: str
    reason: str


class Loss(enum.StrEnum):
    RESPELLED = "respelled"
    FORKED = "forked"
    RECREATABLE_SPELLING = "recreatable-spelling"
    CWD_RELATIVE = "cwd-relative"
```

| 項目 | 決定 |
|---|---|
| 型 | `AnyBinding` は `NamedTuple`。`listener, losses, route, skipped = bind_any(path)` と分解でき、`bind_ex` の `(listener, discard)` と同じくタプルで返す |
| `losses` | `Loss` の並び。`Loss` は `enum.StrEnum` で、`str()` と `.value` が SPEC の名前。SPEC の名前を全部持つ (`FORKED` は sockat-py では付かないが、SPEC の語彙として置く) |
| `route` | `Route` の値 (SPEC の経路の名前の文字列)。`Route` の enum は公開しない |
| `skipped` | 試した順。既定の `bind` の経路を判断した所で、経路の名前と理由を積む (`_route.Trail`) |

`losses` に付くもの (SPEC の「経路の名前」の表の「失うもの」列):

| 経路 | `losses` |
|---|---|
| standard (絶対パス、abstract のアドレス) | 空 |
| standard (相対パス) | `cwd-relative` |
| thread-cwd、at、proc、thread-cwd-sockdir、at-sockdir、proc-sockdir | `respelled` |
| symlink-file | `respelled` |
| symlink-dir (持ち主だけが書ける置き場) | `respelled` |
| symlink-dir (共有の置き場) | `respelled`、`recreatable-spelling` |

- 長い相対パスは D を通る経路に進むので `respelled` だけ (カーネルが覚えるのは `R/<ファイル名>` で、渡した相対パスではない)

`skipped` に入るもの (長いパス):

| 位置 | 経路 | 理由の例 |
|---|---|---|
| 最初 | standard | `path too long` |
| 次 (Linux / Android 以外) | symlink-file | `no place for the symlink`、`symlink not followed by bind` |
| D を通す経路 | この OS にある経路 (Linux は thread-cwd、proc。Apple の OS は thread-cwd。FreeBSD は at) | `spelling too long`、`seccomp filter`、`no pthread_fchdir_np`、`thread cwd turned off`、`/proc unusable`、`thread cwd refused`、`no place for D` |
| fork-cwd | fork-cwd | `no fork route in sockat-py` |
| D 無しの形 (D をどこにも作れない時だけ) | thread-cwd-sockdir (FreeBSD は at-sockdir)、proc-sockdir | 上と同じ、`socket directory not opened`、`R not created` |
| fork-cwd | fork-cwd-sockdir (D 無しの形も使えない時) | `no fork route in sockat-py` |

- 理由は短い英語の句で、診断用。中身は約束しない
- fork-cwd と fork-cwd-sockdir は、SPEC の順でその位置に来た時に「sockat-py に無い」理由で入れる。SPEC の他の言語では fork-cwd で届く所に来たことが、利用者の手元で分かる
- connect も同じ形で経路を積む (`_route.connect_routes`、[DR-0004](DR-0004-own-directory-route.md) の決定 5)。公開 API で読む口は無く (Python に `connect_any` は無い)、試験と適合試験の CLI (`conformance/cli.py`) が読む
- 内部は `_route.reach` / `_route.reach_long` が `Reached(sock, route, losses, skipped)` を返し、`run` / `run_long` はその socket と route だけを返す (既定の API の振る舞いは変えない)

### 4. 失敗した時

| 項目 | 決定 |
|---|---|
| 例外 | `bind` と同じ例外 (`OSError` とそのサブクラス、`ValueError`) を、型も `errno` も変えずに送出する |
| 付けるもの | 例外の属性 `skipped` (`tuple[Skipped, ...]`、失敗するまでに使えなかった経路) と `route` (実際に bind して失敗した経路の名前、そこまで進まなかった時は `None`) |
| `route` が `None` | ディレクトリが無い (`ENOENT`) 等、経路の bind より前に決まるエラー、どの経路も使えない `ENAMETOOLONG`、引数の誤り |

理由: 属性を足すだけなら、`except FileNotFoundError` や `e.errno` で分けている呼び出し側がそのまま動く。

## Alternatives Considered

- 案 A: `sockat` の名前空間に `bind_any` を置く
  - 不採用理由: 補完で既定の `bind` と並び、選ばずに使えてしまう (ハブの DR-0013 の「既定の API に出さない」に反する)
- 案 B: 別の配布物 (`sockat-any` 等) に置く
  - 不採用理由: 純 Python で中身が小さく、配布物を分ける理由が無い。入れ方で API が変わる
- 案 C: extras (`pip install sockat[any]`) で有効にする
  - 不採用理由: extras は依存を足すだけで、コードの有無は変えられない
- 案 D: `AnyBinding` を frozen な dataclass にする (位置で分解できない)
  - 不採用理由: 項目は SPEC で決まり、足すのは `losses` の名前 (値) の側。`bind_ex` と同じくタプルで分解できる方が Python の慣習に合う
- 案 E: 失敗を `OSError` のサブクラス (例: `AnyBindError`) で返す
  - 不採用理由: errno から決まるサブクラス (`FileNotFoundError` 等) にならず、`except FileNotFoundError` が外れる
- 案 F: 失敗を新しい例外で包み、元の例外を `__cause__` に置く
  - 不採用理由: 型が変わり、`bind` と同じ `except` で拾えない
- 案 G: `skipped` に SPEC の fork の経路を入れない
  - 不採用理由: SPEC の順で fork に来た (他の言語なら fork で届いた) ことが見えなくなる

## Alternatives Considered

- 案 A: `sockat` の名前空間に `bind_any` を置く
  - 不採用理由: 補完で既定の `bind` と並び、選ばずに使えてしまう (ハブの DR-0013 の「既定の API に出さない」に反する)
- 案 B: 別の配布物 (`sockat-any` 等) に置く
  - 不採用理由: 純 Python で中身が小さく、配布物を分ける理由が無い。入れ方で API が変わる
- 案 C: extras (`pip install sockat[any]`) で有効にする
  - 不採用理由: extras は依存を足すだけで、コードの有無は変えられない
- 案 D: `AnyBinding` を `typing.NamedTuple` にする (`listener, route, skipped = bind_any(path)` と分解できる)
  - 不採用理由: 失ったものを返す項目を足すと、位置で分解するコードが壊れる
- 案 E: `skipped` に SPEC の fork の経路を入れない
  - 不採用理由: SPEC の順で fork に来た (他の言語なら fork で届いた) ことが見えなくなる

## Consequences

- NetBSD / OpenBSD / illumos と、D を通す経路の手段を使えない Linux / Apple の OS でも、置き場に収まる長さのファイル名なら `bind_any` で届く
- 試験は `tests/test_any.py`。symlink-dir を通す試験は、公開 API が環境から読む設定 (`_route.Config.from_env`) を、既定の `bind` の経路を全部使えなくする設定 (symlink-file の置き場なし・D の置き場なし・スレッド cwd を切る・`/proc` を無いことにする) に差し替えて行う。FreeBSD では at を使えなくできないので行わない

## 未確定

- `bind_any` に後始末つきの形 (`_ex`) を置くか (ハブの DR-0013 の未確定)

## 関連

- [ハブの DR-0013](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0013-any-means-reach-at-any-cost.md)
- [ハブの SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md) (「公開する操作」「symlink-dir」「symlink の置き場と名前」)
- [DR-0001](../DR-0001-python-port-design.md)、[DR-0003](../DR-0003-report-the-given-path.md)、[DR-0004](DR-0004-own-directory-route.md)
