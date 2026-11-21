# sockat

> [English](./README.md) | 日本語

`sun_path` の上限 (NUL 込みで macOS と BSD は 104 バイト、Linux は 108 バイト) を超えるパスでも、Unix domain socket を bind / connect できる Python のライブラリ。

言語によらない振る舞いの正本は [sockat の仕様](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md) で、sockat-py はその Python 実装。

## 使い方

```sh
pip install sockat
```

標準の書き方を sockat に書き換えるだけ。戻り値は標準の `socket.socket` で、短いパスは標準にそのまま渡すので振る舞いは変わらない。

```python
import sockat

# listener = socket.socket(socket.AF_UNIX); listener.bind(path); listener.listen()
listener = sockat.bind(path)

# client = socket.socket(socket.AF_UNIX); client.connect(path)
client = sockat.connect(path)
```

| 関数 | 置き換える標準の書き方 | 戻り値 |
|---|---|---|
| `sockat.bind(path)` | `socket()` → `bind(path)` → `listen()` | listen 済みの `socket.socket` |
| `sockat.connect(path)` | `socket()` → `connect(path)` | `socket.socket` |
| `sockat.connect_nonblocking(path)` | `socket()` → `setblocking(False)` → `connect(path)` | 非ブロッキングの `socket.socket` (asyncio 等のイベントループ向け) |
| `sockat.datagram.bind(path)` / `unbound()` / `connect(sock, path)` | `SOCK_DGRAM` の同じ書き方 | `socket.socket` / `None` |
| `sockat.ex.bind(path, options=...)` など | 同じ + 綴りの性質・片付け・診断 | `Bound` / `Connected` / `Report` ([MANUAL](./docs/MANUAL-ja.md#sockatex)) |
| `sockat.ex.resolve_for_bind` / `resolve_for_connect` | 標準に自分で渡す短い綴り | `ForBind` / `ForConnect` ([MANUAL](./docs/MANUAL-ja.md#resolve)) |
| `sockat.ex.sweep()` / `sweep_base(path)` | — | `None` ([MANUAL](./docs/MANUAL-ja.md#sweep)) |

- `path` は `str` / `bytes` / `os.PathLike`
- Python 3.11 以上。依存なし (純 Python)

## 知っておくこと

- 長いパスで bind すると、`/tmp/sockat-<uid>/` 等に小さなディレクトリと symlink が残る。何が残り何で消えるかは仕様の「[置き場に残る物](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#置き場に残る物)」と「[`sweep`](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#sweep)」。試験を何度も回す環境では `sockat.ex.sweep()` を 1 回呼べば片付く
- 長いパスで標準と違う点は仕様の「[長いパスで標準と違う点](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#長いパスで標準と違う点)」、避ける書き方は「[使ってはいけない書き方](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#使ってはいけない書き方)」 (Python での形と代わりのコードは [MANUAL](./docs/MANUAL-ja.md#使ってはいけない書き方))
- Python に固有の注意 (アドレスを元のパスで返す subclass、asyncio 等) は [MANUAL の「Python に固有の注意」](./docs/MANUAL-ja.md#python-に固有の注意)

## OS ごとの経路

長いパスは、次の順に経路を試し、最初に使えた経路で bind / connect する。経路の名前と中身は仕様の「[経路の名前](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md#経路の名前)」、どこまで届くかは[届く範囲の表](https://github.com/kawaz/sockat/blob/main/pages/reach/README-ja.md)。sockat-py は仕様の経路をすべて作っている (fork はしない)。

| OS | `bind` | `connect` / `connect_nonblocking` |
|---|---|---|
| Linux / Android (`/proc` を使える) | link-dir (procB → B の実パス) → thread-cwd | proc-direct → thread-cwd-direct → link-file |
| Linux / Android (`/proc` を使えない) | link-dir (B の実パス) → thread-cwd | relative → thread-cwd-direct → link-file |
| macOS / iOS | link-dir (B の実パス) → link-file → thread-cwd | relative → widened → thread-cwd-direct → link-file |
| FreeBSD | link-dir (B の実パス) → at | at-direct → link-file |
| NetBSD | link-dir (B の実パス) → link-file | relative → link-file |
| OpenBSD / illumos | link-dir (B の実パス) | relative → link-file |
| その他の Unix | link-dir (B の実パス) → link-file (辿るかを実行時に見分ける) | relative → link-file |
| Windows | 対象外 (import で `ImportError`) | 同左 |

## ドキュメント

- [docs/MANUAL-ja.md](./docs/MANUAL-ja.md) — Python での名前と型、使ってはいけない書き方の Python での形、Python に固有の注意、エラーの型
- [sockat の仕様](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md) — 言語によらない振る舞い ([経路の作り方](https://github.com/kawaz/sockat/blob/main/docs/SPEC-routes-ja.md)、[言語ごとの注意](https://github.com/kawaz/sockat/blob/main/pages/langs-ja.md))
- [docs/decisions/](./docs/decisions/INDEX.md) — Python 版の設計の判断 (DR)

## ライセンス

MIT License, Yoshiaki Kawazu (@kawaz)
