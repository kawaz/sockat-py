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
| [DR-0002](DR-0002-unsafe-base-held-by-fd.md) | ✅ 実装済 | 安全でない置き場を fd で掴む形 (/proc を使える Linux の connect)。ハブの DR-0008 の Python での作り方 |
| [DR-0003](DR-0003-report-the-given-path.md) | ✅ 実装済 | 長いパスのソケットはアドレスを元のパスで返す (`socket.socket` の subclass)。asyncio の後始末が短いパスと同じに動く |

振る舞いの正本はハブの [SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)、経路の設計の判断は [sockat-rs の DR-0001](https://github.com/kawaz/sockat-rs/blob/main/docs/decisions/DR-0001-long-socket-path-routes.md) にある。
