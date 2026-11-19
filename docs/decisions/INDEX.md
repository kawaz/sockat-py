# Decision Records 一覧

Status は各 DR ファイルの `Status:` 行が正本。

状態は絵文字とラベルだけ。日付・Phase・裁定の内訳は各 DR 本文に書く。

- `💭 提案`: Decision がまだ裁定されていない。実装に着手しない (本文の `Status: Proposed`)
- `✅ 実装済`: Decision の全部に実装エビデンスがある
- `🟡 部分実装`: 一部のみ実装
- `⬜ 未実装`: 裁定済みで設計のみ、実装エビデンスなし (着手してよい)
- `🚧 進行中`: 実装の途中
- `N/A`: 実装対象でない (命名・思想・プロセス等)
- `❌ 撤退`: 撤退判断済

| DR | 状態 | 説明 |
|---|---|---|
| [DR-0001](DR-0001-python-port-design.md) | ✅ 実装済 | Python 版の作り方。純 Python + ctypes で fork 以外の経路を作る、`bind` / `connect` / `connect_nonblocking` が `socket.socket` を返す、標準に揃える点、NUL、エラーの型、Python 3.11 以上 |
| [DR-0002](DR-0002-unsafe-base-held-by-fd.md) | ✅ 実装済 | proc-held (/proc を使える Linux の connect の経路)。D の中のソケットのファイルを指す `s` を `/proc/self/fd/<D>/s` で connect する |
| [DR-0003](DR-0003-report-the-given-path.md) | ✅ 実装済 | 長いパスのソケットはアドレスを元のパスで返す (`socket.socket` の subclass)。asyncio の後始末が短いパスと同じに動く |
| [DR-0004](DR-0004-own-directory-route.md) | ✅ 実装済 | D を通す経路 (D を fd で掴み R を通す `R/<ファイル名>`) の Python での作り方。D の置き場と開き方 (開けない D は名前で確かめて消す、D12-Q5)、経路 (thread-cwd / at / proc)、D をどこにも作れない bind の D 無しの形 (thread-cwd-sockdir / at-sockdir / proc-sockdir)、それでも届かない `bind` は `ENAMETOOLONG` (D12-Q3)、connect の順 (書き込みもスレッドも要らない経路を先に、D は 1 回だけ作って共有)、bind の symlink-file (DR-0016)、bind の `discard` (listener を閉じない、D12-Q4) |
| [DR-0005](DR-0005-bind-any.md) | ❌ 撤退 | DR-0006 に置き換え。`bind_any` の Python での形。`sockat.any` に置き `import sockat` では読み込まない、最後に置き場の symlink (持ち主だけが書ける置き場 → 共有の置き場)、戻り値 `AnyBinding(listener, losses, route, skipped)` (`Loss` は SPEC の名前の `StrEnum`)、失敗は同じ例外に `skipped` と `route` の属性 |
| [DR-0006](DR-0006-sockat-ex.md) | ✅ 実装済 | `sockat.ex` (ハブの DR-0015 の Python での形)。`import sockat` では読み込まない `sockat.ex` に `bind` / `connect` / `connect_nonblocking` (引数 `last_resort`)、戻り値 `Bound(listener, discard, losses, route, skipped)` / `Connected(sock, losses, route, skipped)`、bind の最終手段は symlink-dir、connect の最終手段の fork-cwd-direct は `skipped` に入れるだけ、失敗は同じ例外に `skipped` と `route` の属性 |

振る舞いの正本はハブの [SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)、経路の設計の判断はハブの [DR-0012](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0012-own-directory-and-thread-cwd.md) にある。
