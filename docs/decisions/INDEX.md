# Decision Records 一覧

Status は各 DR ファイルの `Status:` 行が正本。ここに載るのは今立っている DR だけで、置き換えられた DR は [archive/INDEX.md](archive/INDEX.md) にある。

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
| [DR-0001](DR-0001-python-port-design.md) | ✅ 実装済 | Python 版の作り方 (ハブの DR-0017)。純 Python + ctypes で fork 以外の経路を作る、置き場 B・D・E (realpath、flock、sweep との競合、fork の子)、公開 API (`sockat`・`sockat.datagram`・`sockat.ex`・`sockat.ex.datagram`、`Options`・`Spelling`・`SocketFile` ほか)、標準に揃える点、NUL、エラーの型、Python 3.11 以上 |
| [DR-0003](DR-0003-report-the-given-path.md) | ✅ 実装済 | 長いパスのソケットはアドレスを元のパスで返す (`socket.socket` の subclass)。asyncio の後始末が短いパスと同じに動く |

振る舞いの正本はハブの [SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)、今の設計の判断はハブの [DR-0017](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0017-resolve-through-persistent-base.md) にある。
