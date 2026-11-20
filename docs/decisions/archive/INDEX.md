# 置き換えられた Decision Records

今の設計の DR は [../INDEX.md](../INDEX.md)。ここにあるのは置き換えられた DR で、今の実装の根拠には使わない。

| DR | 置き換えた DR | 説明 |
|---|---|---|
| [DR-0002](DR-0002-unsafe-base-held-by-fd.md) | [DR-0001](../DR-0001-python-port-design.md) | proc-held (D の中のソケットのファイルを指す `s` を `/proc/self/fd/<D>/s` で connect する経路) |
| [DR-0004](DR-0004-own-directory-route.md) | [DR-0001](../DR-0001-python-port-design.md) | 呼び出しごとの D と R を通す経路 (thread-cwd / at / proc)、D 無しの形、bind の symlink-file、`discard` |
| [DR-0005](DR-0005-bind-any.md) | [DR-0001](../DR-0001-python-port-design.md) | `bind_any` と `sockat.any` |
| [DR-0006](DR-0006-sockat-ex.md) | [DR-0001](../DR-0001-python-port-design.md) | `sockat.ex` の `last_resort`、`losses`、`discard` |
